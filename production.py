#!/usr/bin/env python3
"""Offline production: shot sheets, local media, narration, previews and cuts.

Creative decisions come from the author/agent in shots.json. This program never
calls a generation service or treats a static preview as an AI-generated video.
"""

import argparse
import copy
import csv
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

import workflow as core

Error = core.WorkflowError
ROOT = Path(__file__).resolve().parent
STORY_FIELDS = ("narrative_goal", "framing", "action", "camera", "image_prompt")


def write_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def tool(name):
    for candidate in (ROOT / ".tools/bin" / name, Path("/opt/homebrew/opt/ffmpeg-full/bin") / name,
                      Path("/usr/local/opt/ffmpeg-full/bin") / name, Path("/opt/homebrew/bin") / name,
                      Path("/usr/local/bin") / name):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    found = shutil.which(name)
    if not found:
        raise Error("Install {} before using media commands".format(name))
    return found


def execute(arguments, timeout=600, cwd=None, reject_missing_glyphs=False):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise Error("Local media command timed out; no remote task was submitted")
    if result.returncode:
        raise Error("{} failed: {}".format(Path(arguments[0]).name, result.stderr[-2000:]))
    if reject_missing_glyphs and "failed to find any fallback with glyph" in result.stderr:
        raise Error("Subtitle font is missing required characters; the captioned output was not accepted")
    return result.stdout


def subtitle_font():
    candidates = (
        ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "Arial Unicode MS"),
        ("/Library/Fonts/Arial Unicode.ttf", "Arial Unicode MS"),
        ("/System/Library/Fonts/STHeiti Medium.ttc", "Heiti SC"),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "Noto Sans CJK SC"),
    )
    for file, family in candidates:
        if Path(file).is_file():
            return Path(file), family
    raise Error("Install a Chinese-capable font (Arial Unicode / Heiti / Noto Sans CJK) before burning subtitles")


def finite(value, label, positive=False):
    if isinstance(value, bool):
        raise Error(label + " must be a finite number")
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise Error(label + " must be a finite number")
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise Error(label + " must be a finite " + ("positive" if positive else "nonnegative") + " number")
    return result


def inspect_media(path, kind=None, decode=False):
    path = Path(path).resolve()
    if not path.is_file() or not path.stat().st_size:
        raise Error("Media is missing or empty: " + str(path))
    try:
        raw = json.loads(execute([tool("ffprobe"), "-v", "error", "-show_streams",
                                 "-show_format", "-of", "json", str(path)], timeout=60))
    except ValueError:
        raise Error("ffprobe returned invalid JSON")
    streams = raw.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    image_format = raw.get("format", {}).get("format_name", "")
    is_image = bool(video and (image_format in ("image2", "png_pipe", "jpeg_pipe", "webp_pipe", "bmp_pipe", "tiff_pipe")))
    detected = "image" if is_image else "video" if video else "audio" if audio else None
    if detected is None or (kind and detected != kind):
        raise Error("Expected {}, found {}: {}".format(kind or "media", detected, path))
    duration = None
    video_duration = None
    if detected != "image":
        primary = video if detected == "video" else audio
        duration = finite(raw.get("format", {}).get("duration") or primary.get("duration"), "media duration", True)
        if video:
            video_duration = finite(video.get("duration") or duration, "video duration", True)
    if video and (int(video.get("width", 0)) <= 0 or int(video.get("height", 0)) <= 0):
        raise Error("Media has no valid image dimensions")
    if decode:
        execute([tool("ffmpeg"), "-nostdin", "-v", "error", "-xerror", "-i", str(path),
                 "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"], timeout=600)
    return {"file": str(path), "kind": detected, "duration": duration, "video_duration": video_duration,
            "width": video.get("width") if video else None, "height": video.get("height") if video else None,
            "video_codec": video.get("codec_name") if video else None,
            "audio_codec": audio.get("codec_name") if audio else None,
            "size": path.stat().st_size, "sha256": core.sha256(path), "decoded": decode}


def verified(record):
    if not isinstance(record, dict) or not record.get("file") or not record.get("sha256"):
        raise Error("Media record is missing its file or checksum")
    path = Path(record["file"])
    if not path.is_file() or core.sha256(path) != record["sha256"]:
        raise Error("Registered media was removed or changed: " + str(path))
    return path


def plan_shots(episode):
    by_key = {core.shot_key(s): s for s in episode["shots"]}
    order = episode.get("edit_order", list(by_key))
    if not isinstance(order, list) or not order or any(not isinstance(k, str) or k not in by_key for k in order):
        raise Error("edit_order must be a nonempty list of existing shot@revision keys")
    shots = [by_key[k] for k in order]
    if len({s["shot_id"] for s in shots}) != len(shots):
        raise Error("Choose one revision per shot in edit_order; do not concatenate alternatives")
    return shots


def shot_text(shot):
    value = shot.get("audio", {})
    if not isinstance(value, dict) or not isinstance(value.get("text", ""), str):
        raise Error(core.shot_key(shot) + ": audio must be an object with optional text/file")
    subtitle = shot.get("subtitle", value.get("text", ""))
    if not isinstance(subtitle, str):
        raise Error("subtitle must be text")
    return subtitle.strip()


def frame_duration(shot, fps):
    frames = max(1, round(finite(shot["planned_edit_seconds"], "planned_edit_seconds", True) * fps))
    return frames / fps


def srt_time(seconds):
    total = round(seconds * 1000)
    hours, rest = divmod(total, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, millis = divmod(rest, 1000)
    return "{:02}:{:02}:{:02},{:03}".format(hours, minutes, seconds, millis)


def subtitles(shots, fps):
    output, offset = [], 0.0
    for shot in shots:
        end = offset + frame_duration(shot, fps)
        text = shot_text(shot)
        if text:
            output.append("{}\n{} --> {}\n{}\n".format(len(output) + 1, srt_time(offset), srt_time(end), text))
        offset = end
    return "\n".join(output)


def export_dir(directory, label):
    destination = directory / "exports" / (label + "-" + uuid.uuid4().hex[:10])
    destination.mkdir(parents=True)
    return destination


def storyboard(directory, project, episode, destination=None, fps=24):
    shots = plan_shots(episode)
    for shot in shots:
        missing = [name for name in STORY_FIELDS if not isinstance(shot.get(name), str) or not shot[name].strip()]
        if not isinstance(shot["video_request"].get("prompt"), str) or not shot["video_request"]["prompt"].strip():
            missing.append("video_request.prompt")
        if missing:
            raise Error(core.shot_key(shot) + " missing storyboard fields: " + ", ".join(missing))
        if not isinstance(shot.get("acceptance"), list) or not shot["acceptance"] or any(not isinstance(x, str) for x in shot["acceptance"]):
            raise Error(core.shot_key(shot) + " requires acceptance criteria")
        shot_text(shot)
    destination = destination or export_dir(directory, "storyboard")
    fields = ["shot_id", "revision", "narrative_goal", "framing", "action", "camera",
              "planned_edit_seconds", "continuity_in", "continuity_out", "image_prompt", "video_prompt", "subtitle"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    lines = ["# " + project.get("name", project["project_id"]), "",
             "分镜由创作者/Agent设计；本程序检查字段并导出。内容与连续性仍需审阅。", ""]
    prompts = []
    for shot in shots:
        row = {field: shot.get(field, "") for field in fields}
        row.update(video_prompt=shot["video_request"]["prompt"], subtitle=shot_text(shot))
        # Spreadsheet programs must not evaluate user-authored prompts as formulae.
        writer.writerow({key: "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value
                         for key, value in row.items()})
        lines += ["## " + core.shot_key(shot), "", "- 用途：" + shot["narrative_goal"],
                  "- 景别：" + shot["framing"], "- 动作：" + shot["action"], "- 运镜：" + shot["camera"],
                  "- 目标时长：{} 秒".format(shot["planned_edit_seconds"]),
                  "- 接镜：{} → {}".format(shot.get("continuity_in", "待确认"), shot.get("continuity_out", "待确认")),
                  "- 参考：" + ", ".join(str(r.get("file") or r.get("resource_id") or "待制作") for r in shot["references"]),
                  "- 验收：" + "；".join(shot["acceptance"]), ""]
        prompts += ["## " + core.shot_key(shot), "", "### 图片", "", shot["image_prompt"], "",
                    "### 视频", "", shot["video_request"]["prompt"], ""]
    (destination / "storyboard.csv").write_text(buffer.getvalue(), encoding="utf-8-sig")
    (destination / "storyboard.md").write_text("\n".join(lines), encoding="utf-8")
    (destination / "prompts.md").write_text("\n".join(prompts), encoding="utf-8")
    (destination / "subtitles.srt").write_text(subtitles(shots, fps), encoding="utf-8")
    write_json(destination / "shots.json", episode)
    return {"directory": str(destination), "shots": len(shots),
            "duration": sum(frame_duration(s, fps) for s in shots)}


def immutable_copy(source, folder, kind):
    info = inspect_media(source, kind, decode=True)
    folder.mkdir(parents=True, exist_ok=True)
    suffix = Path(source).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".media"
    target = folder / (info["sha256"][:16] + suffix)
    if target.exists():
        if core.sha256(target) != info["sha256"]:
            raise Error("Existing asset differs; preserve it and inspect before retrying")
    else:
        with target.open("xb") as output, Path(source).open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
        if core.sha256(target) != info["sha256"]:
            raise Error("Source changed during import; incomplete copy was not registered")
    info["file"] = str(target.resolve())
    return info


def content_snapshot(directory, shot):
    refs = []
    for ref in shot["references"]:
        item = copy.deepcopy(ref)
        if ref.get("file"):
            path = (directory / ref["file"]).resolve()
            if not path.is_file():
                raise Error("Missing reference: " + str(path))
            item["sha256"] = core.sha256(path)
        refs.append(item)
    return {"video_request": copy.deepcopy(shot["video_request"]), "references": refs}


def import_media(directory, episode, state, shot, source, kind, origin):
    key = core.shot_key(shot)
    if kind == "image" and (key in state["operations"] or key in state.get("local_results", {})):
        raise Error("Create a new shot revision before replacing a generation reference")
    collection = state.setdefault("local_results" if kind == "video" else "assets", {})
    record_key = key if kind == "video" else key + "#" + kind
    previous = collection.get(record_key)
    if previous:
        verified(previous)
        if core.sha256(Path(source)) == previous["sha256"]:
            return previous
        raise Error("This media version already exists; create a new revision")
    record = immutable_copy(source, directory / "media" / key / kind, kind)
    record.update(source=origin, imported_at=core.timestamp())
    if kind == "video":
        record["input"] = content_snapshot(directory, shot)
        record["playback_review"] = "pending"
        record["external_cost"] = "unknown"
    elif kind == "image":
        # Replace the first-frame slot while retaining all other references.
        shot["references"] = [{"file": str(Path(record["file"]).relative_to(directory)),
                               "resource_id": None, "purpose": "已导入首帧"}] + shot["references"][1:]
        write_json(directory / "shots.json", episode)
    else:
        shot.setdefault("audio", {})["file"] = str(Path(record["file"]).relative_to(directory))
        write_json(directory / "shots.json", episode)
    collection[record_key] = record
    core.write_state(directory, state)
    return record


def local_result(directory, state, shot):
    key = core.shot_key(shot)
    record = state.get("local_results", {}).get(key)
    if record:
        if record.get("input") != content_snapshot(directory, shot):
            raise Error(key + ": generation inputs changed; create a new revision")
    else:
        record = state["operations"].get(key)
        if not record or record.get("phase") != "downloaded":
            raise Error(key + ": import or download a video first")
        # Check only generation content; editing timing/audio is independent.
        saved = record.get("input", {})
        if saved.get("video_request") != shot["video_request"]:
            raise Error(key + ": generation prompt/settings changed")
        if saved.get("references") != core.snapshot(directory, shot)["references"]:
            raise Error(key + ": generation references changed")
    verified(record)
    return record


def select(directory, state, shot, note):
    record = local_result(directory, state, shot)
    info = inspect_media(record["file"], "video", decode=True)
    result = {"revision": shot["revision"], "file": record["file"], "sha256": info["sha256"],
              "resource_id": record.get("resource_id"), "source": record.get("source", "dreamina-canvas"),
              "note": note, "selected_at": core.timestamp()}
    state["selected"][shot["shot_id"]] = result
    core.write_state(directory, state)
    return result


def revise(directory, episode, shot, revision):
    core.identifier(revision, "revision")
    key = shot["shot_id"] + "@" + revision
    if any(core.shot_key(s) == key for s in episode["shots"]):
        raise Error("Revision already exists: " + key)
    if "edit_order" not in episode:
        episode["edit_order"] = [core.shot_key(s) for s in plan_shots(episode)]
    new = copy.deepcopy(shot)
    new["revision"] = revision
    episode["shots"].append(new)
    write_json(directory / "shots.json", episode)
    return {"created": key, "note": "Edit the new revision, then explicitly update edit_order and select its video"}


def narration(directory, episode, state, shot, voice):
    shot_text(shot)
    text = shot.get("audio", {}).get("text", "").strip()
    if not text:
        raise Error("Fill audio.text before generating temporary narration")
    voice = voice or shot.get("audio", {}).get("voice", "Tingting")
    if sys.platform != "darwin":
        raise Error("Local narration uses macOS say; on other systems import an audio file")
    previous = state.get("assets", {}).get(core.shot_key(shot) + "#audio")
    origin = "macOS say / " + voice
    if previous:
        if previous.get("narration_text") == text and previous.get("source") == origin:
            verified(previous)
            return previous
        raise Error("Narration already exists; create a new revision to change it")
    with tempfile.TemporaryDirectory(prefix="narration-", dir=str(directory)) as temporary:
        text_path = Path(temporary) / "text.txt"
        text_path.write_text(text, encoding="utf-8")
        aiff, wav = Path(temporary) / "speech.aiff", Path(temporary) / "speech.wav"
        execute([tool("say"), "-v", voice, "-f", str(text_path), "-o", str(aiff)], timeout=120)
        execute([tool("ffmpeg"), "-nostdin", "-v", "error", "-n", "-i", str(aiff),
                 "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(wav)])
        record = import_media(directory, episode, state, shot, wav, "audio", origin)
    record["narration_text"] = text
    core.write_state(directory, state)
    return record


def reference_image(directory, state, shot):
    refs = [ref for ref in shot["references"] if ref.get("file")]
    if not refs:
        raise Error(core.shot_key(shot) + ": preview needs a local image reference")
    path = (directory / refs[0]["file"]).resolve()
    for record in state.get("assets", {}).values():
        if record.get("file") == str(path):
            verified(record)
    return inspect_media(path, "image", decode=True)


def render(directory, project, episode, state, mode, size, fps, music=None, music_volume=0.15,
           burn_subtitles=False, narration_gain_db=0):
    match = re.fullmatch(r"(\d+)x(\d+)", size)
    if not match:
        raise Error("--size must be WIDTHxHEIGHT")
    width, height = map(int, match.groups())
    if not (16 <= width <= 3840 and 16 <= height <= 3840) or width % 2 or height % 2:
        raise Error("Output dimensions must be even and between 16 and 3840")
    if not 1 <= fps <= 60:
        raise Error("fps must be between 1 and 60")
    volume = finite(music_volume, "music volume")
    if volume > 1:
        raise Error("music volume must be between 0 and 1")
    if isinstance(narration_gain_db, bool) or not math.isfinite(narration_gain_db) or not -12 <= narration_gain_db <= 12:
        raise Error("narration gain must be between -12 and 12 dB")
    ffmpeg = tool("ffmpeg")
    if burn_subtitles and not re.search(r"\bsubtitles\s+V->V", execute([ffmpeg, "-hide_banner", "-filters"])):
        raise Error("Subtitle burn-in requires FFmpeg with libass; on macOS install ffmpeg-full")
    font = subtitle_font() if burn_subtitles else None
    shots, sources = plan_shots(episode), []
    for shot in shots:
        shot_text(shot)
        provenance = {"source": "local still image", "generation_cost": "not incurred by this render"}
        duration = frame_duration(shot, fps)
        start = finite(shot.get("edit_in_seconds", 0), "edit_in_seconds")
        still = mode == "preview" or shot.get("render_mode") == "still"
        if still:
            info = reference_image(directory, state, shot)
        else:
            chosen = state["selected"].get(shot["shot_id"])
            if not chosen or chosen.get("revision") != shot["revision"]:
                raise Error(core.shot_key(shot) + ": explicitly select this video revision before rendering")
            record = local_result(directory, state, shot)
            provenance = {field: record.get(field) for field in
                          ("source", "resource_id", "submit_id", "quote_credits", "reserved_credits", "actual_credits", "external_cost")}
            provenance["source"] = record.get("source", "dreamina-canvas")
            if chosen.get("file") != record["file"] or (chosen.get("sha256") and chosen["sha256"] != record["sha256"]):
                raise Error("Selected video no longer matches the stored result")
            info = inspect_media(record["file"], "video", decode=True)
            if start + duration > info["video_duration"] + 0.5 / fps:
                raise Error(core.shot_key(shot) + ": selected clip is shorter than the planned trim")
        audio = None
        if shot.get("audio", {}).get("file"):
            path = (directory / shot["audio"]["file"]).resolve()
            for registered in state.get("assets", {}).values():
                if registered["file"] == str(path):
                    verified(registered)
                    if "narration_text" in registered and registered["narration_text"] != shot["audio"].get("text", "").strip():
                        raise Error(core.shot_key(shot) + ": narration text changed; create/import matching audio")
            audio = inspect_media(path, "audio", decode=True)
            if audio["duration"] > duration + 0.5 / fps:
                raise Error(core.shot_key(shot) + ": narration exceeds the shot; lengthen the edit or revise the audio")
        elif shot.get("audio", {}).get("text", "").strip():
            raise Error(core.shot_key(shot) + ": narration text has no audio; run narrate or import audio")
        sources.append({"key": core.shot_key(shot), "media": info, "audio": audio, "still": still,
                        "in_seconds": start, "duration": duration, "provenance": provenance})
    music_info = inspect_media(music, "audio", decode=True) if music else None
    destination = export_dir(directory, mode)
    storyboard(directory, project, episode, destination, fps)
    total = sum(source["duration"] for source in sources)
    with tempfile.TemporaryDirectory(prefix=".render-", dir=str(directory)) as temporary:
        work = Path(temporary)
        for index, source in enumerate(sources):
            print("Rendering {}/{} {}".format(index + 1, len(sources), source["key"]), file=sys.stderr, flush=True)
            command = [ffmpeg, "-nostdin", "-v", "error", "-n"]
            if source["still"]:
                command += ["-loop", "1", "-framerate", str(fps)]
            else:
                command += ["-ss", str(source["in_seconds"])]
            command += ["-i", source["media"]["file"]]
            if source["audio"]:
                command += ["-i", source["audio"]["file"]]
                audio_map = "1:a:0"
            elif not source["still"] and source["media"]["audio_codec"]:
                audio_map = "0:a:0"
            else:
                command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
                audio_map = "1:a:0"
            filters = "scale={}:{}:force_original_aspect_ratio=decrease,pad={}:{}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={},format=yuv420p".format(width, height, width, height, fps)
            audio_filters = "aresample=48000"
            if source["audio"] and narration_gain_db:
                audio_filters += ",volume={}dB,alimiter=limit=0.95:level=0:latency=1".format(narration_gain_db)
            audio_filters += ",apad"
            command += ["-map", "0:v:0", "-map", audio_map, "-vf", filters,
                        "-af", audio_filters, "-t", str(source["duration"]),
                        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "aac",
                        "-ar", "48000", "-ac", "2", "-video_track_timescale", "24000",
                        "-movflags", "+faststart", str(work / "{:04}.mp4".format(index))]
            execute(command)
        (work / "concat.txt").write_text("".join("file '{:04}.mp4'\n".format(i) for i in range(len(sources))), encoding="utf-8")
        joined = work / "joined.mp4"
        execute([ffmpeg, "-nostdin", "-v", "error", "-n", "-f", "concat", "-safe", "1", "-i", str(work / "concat.txt"),
                 "-c", "copy", "-movflags", "+faststart", str(joined)])
        output = destination / "video.mp4"
        if music_info:
            execute([ffmpeg, "-nostdin", "-v", "error", "-n", "-i", str(joined), "-stream_loop", "-1", "-i", music_info["file"],
                     "-filter_complex", "[1:a]volume={}[bg];[0:a][bg]amix=inputs=2:duration=first:normalize=0[a]".format(volume),
                     "-map", "0:v:0", "-map", "[a]", "-t", str(total), "-c:v", "copy", "-c:a", "aac",
                     "-movflags", "+faststart", str(output)])
        else:
            shutil.copy2(joined, output)
        if burn_subtitles and (destination / "subtitles.srt").stat().st_size:
            # A fixed filename and explicit cwd keep arbitrary project paths out of the filter expression.
            # Load the font file directly: CoreText can resolve PingFang to an unreadable reserved font.
            (work / "fonts").mkdir()
            shutil.copyfile(font[0], work / "fonts" / ("subtitle" + font[0].suffix))
            shutil.copy2(destination / "subtitles.srt", work / "subtitles.srt")
            captioned = work / "captioned.mp4"
            execute([ffmpeg, "-nostdin", "-v", "warning", "-n", "-i", str(output),
                     "-vf", "subtitles=filename=subtitles.srt:fontsdir=fonts:force_style='FontName={},FontSize=12,Outline=1,Shadow=0,MarginV=24'".format(font[1]),
                     "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "copy",
                     "-movflags", "+faststart", str(captioned)], cwd=str(work), reject_missing_glyphs=True)
            shutil.copy2(captioned, output)
    result = inspect_media(output, "video", decode=True)
    if abs(result["duration"] - total) > max(0.1, 2 / fps):
        raise Error("Rendered duration differs from the planned timeline; inspect the output")
    manifest = {"project_id": project["project_id"], "episode_id": episode.get("episode_id"),
                "mode": mode, "created_at": core.timestamp(), "fps": fps, "sources": sources,
                "music": music_info, "music_volume": volume if music else None,
                "subtitles_burned": burn_subtitles and bool((destination / "subtitles.srt").stat().st_size),
                "subtitle_font": font[1] if font else None,
                "narration_gain_db": narration_gain_db,
                "video": result, "subtitle_sha256": core.sha256(destination / "subtitles.srt"),
                "review": "pending", "note": "Static animatic" if mode == "preview" else "Rough cut; human playback review required"}
    manifest["document_sha256"] = {name: core.sha256(destination / name) for name in
                                  ("storyboard.csv", "storyboard.md", "prompts.md", "shots.json")}
    write_json(destination / "render.json", manifest)
    return {"directory": str(destination), "video": result, "mode": mode}


def package(directory, project, render_directory):
    render_directory = Path(render_directory).resolve()
    manifest = core.read_json(render_directory / "render.json")
    if manifest.get("project_id") != project["project_id"]:
        raise Error("Render belongs to another project")
    video = render_directory / "video.mp4"
    if core.sha256(video) != manifest["video"]["sha256"]:
        raise Error("Rendered video changed after verification")
    if core.sha256(render_directory / "subtitles.srt") != manifest["subtitle_sha256"]:
        raise Error("Subtitles changed after rendering")
    for filename, checksum in manifest.get("document_sha256", {}).items():
        if filename not in ("storyboard.csv", "storyboard.md", "prompts.md", "shots.json"):
            raise Error("Unexpected render document name")
        if core.sha256(render_directory / filename) != checksum:
            raise Error("Render document changed: " + filename)
    inspect_media(video, "video", decode=True)
    destination = export_dir(directory, "delivery")
    files = ("video.mp4", "subtitles.srt", "storyboard.csv", "storyboard.md", "prompts.md", "shots.json", "render.json")
    checksums = {}
    for filename in files:
        source = render_directory / filename
        if not source.is_file():
            raise Error("Render package is incomplete: " + filename)
        shutil.copy2(source, destination / filename)
        checksums[filename] = core.sha256(destination / filename)
    write_json(destination / "manifest.json", {"project_id": project["project_id"], "purpose": "internal_review",
               "render_mode": manifest["mode"], "files": checksums, "created_at": core.timestamp()})
    (destination / "README.md").write_text(
        "# 内部审阅包\n\n本包是{}，不代表正式上架验收通过。\n\n"
        "- video.mp4：{}；subtitles.srt：逐镜头字幕，可导入剪映精调。\n"
        "- storyboard/prompts/shots：本次合成时的分镜、提示词与输入快照。\n"
        "- render.json：媒体来源、版本、校验值和实际时长；manifest.json：本包文件校验值。\n\n"
        "## 人工检查\n\n- [ ] 完整播放：叙事、接镜、人物/道具连续性、音量与字幕时点。\n"
        "- [ ] 创作者确认选用版本、封面、剧名与简介。\n"
        "- [ ] 正式发布前按当前平台规则确认体裁、AI声明、版权材料、合同与备案。\n"
        "- [ ] 漫剧正式首次上传包和整剧至少20分钟；用 audit 检查实际文件，不以计划时长替代。\n".format(
            "静态分镜预演" if manifest["mode"] == "preview" else "粗剪",
            "已烧录字幕的视频" if manifest.get("subtitles_burned") else "无烧录字幕的视频"), encoding="utf-8")
    return {"directory": str(destination), "purpose": "internal_review", "files": checksums}


def audit(files, kind, high_budget=False):
    infos, problems, hashes = [], [], set()
    for file in files:
        info = inspect_media(file, "video", decode=True)
        infos.append(info)
        name, duration = Path(file).name, info["duration"]
        if info["sha256"] in hashes:
            problems.append(name + ": duplicate episode file")
        hashes.add(info["sha256"])
        if info["size"] > 3_000_000_000:
            problems.append(name + ": exceeds conservative 3 GB file limit")
        if kind == "comic":
            if duration < 30:
                problems.append(name + ": comic episode shorter than 30 seconds")
            if (high_budget and duration >= 1200) or (not high_budget and duration > 600):
                problems.append(name + ": comic episode exceeds the selected production-budget duration limit")
        elif duration > 180:
            problems.append(name + ": live-action episode longer than 3 minutes")
    total = sum(info["duration"] for info in infos)
    if kind == "comic" and total < 1200:
        problems.append("Comic first-upload/full-series package must be at least 1200 seconds")
    return {"passed_media_limits": not problems, "kind": kind, "duration": total, "files": infos,
            "problems": problems, "basis": "User-provided creator-center manual; recheck current platform rules before publishing",
            "remaining_manual_checks": ["episode order and missing episodes", "full playback", "rights/contract/filing", "current channel review"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("storyboard", help="Validate authored shots and export Markdown/CSV/prompts/SRT")
    for name in ("import", "select", "revise", "narrate"):
        child = sub.add_parser(name)
        child.add_argument("--shot")
        child.add_argument("--revision")
        if name == "import":
            child.add_argument("--kind", choices=("image", "video", "audio"), required=True)
            child.add_argument("--file", type=Path, required=True)
            child.add_argument("--source", required=True, help="Where the file came from; external cost stays unknown")
        elif name == "select":
            child.add_argument("--note", required=True, help="Playback review/selection note")
        elif name == "revise":
            child.add_argument("--to", required=True)
        else:
            child.add_argument("--voice")
    child = sub.add_parser("render", help="Build a static preview or a cut from explicitly selected video clips")
    child.add_argument("--mode", choices=("preview", "rough-cut"), required=True)
    child.add_argument("--size", default="720x1280")
    child.add_argument("--fps", type=int, default=24)
    child.add_argument("--music", type=Path)
    child.add_argument("--music-volume", type=float, default=0.15)
    child.add_argument("--burn-subtitles", action="store_true", help="Render visible subtitles into the video (requires libass)")
    child.add_argument("--narration-gain-db", type=float, default=0, help="Gain for independent narration tracks, from -12 to +12 dB")
    child = sub.add_parser("package", help="Export a checked internal-review package from an existing render")
    child.add_argument("--render-dir", type=Path, required=True)
    child = sub.add_parser("inspect", help="Probe and fully decode one local media file")
    child.add_argument("--file", type=Path, required=True)
    child = sub.add_parser("audit", help="Check actual episode files against the supplied upload-manual limits")
    child.add_argument("--kind", choices=("comic", "live"), required=True)
    child.add_argument("--high-budget", action="store_true", help="Comic production budget >= CNY 300,000")
    child.add_argument("files", type=Path, nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_media(args.file, decode=True)
        elif args.command == "audit":
            result = audit(args.files, args.kind, args.high_budget)
        else:
            if not args.project_dir:
                raise Error("--project-dir is required for this command")
            directory = args.project_dir.resolve()
            project, episode = core.load_inputs(directory)
            with core.project_lock(directory):
                state = core.load_state(directory, project)
                if args.command == "storyboard":
                    result = storyboard(directory, project, episode)
                elif args.command == "render":
                    result = render(directory, project, episode, state, args.mode, args.size, args.fps, args.music, args.music_volume,
                                    args.burn_subtitles, args.narration_gain_db)
                elif args.command == "package":
                    result = package(directory, project, args.render_dir)
                else:
                    shot = core.choose_shot(episode, args.shot, args.revision)
                    if args.command == "import":
                        result = import_media(directory, episode, state, shot, args.file.resolve(), args.kind, args.source)
                    elif args.command == "select":
                        result = select(directory, state, shot, args.note)
                    elif args.command == "revise":
                        result = revise(directory, episode, shot, args.to)
                    else:
                        result = narration(directory, episode, state, shot, args.voice)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 2 if args.command == "audit" and not result["passed_media_limits"] else 0
    except (Error, OSError) as exc:
        print("production: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
