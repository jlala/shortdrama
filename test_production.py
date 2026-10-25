import copy
import array
import csv
import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import production as p
import workflow as core


class ProductionFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="production-test-")
        self.directory = Path(self.temporary.name).resolve()
        self.project = core.read_json(p.ROOT / "examples/coffee-morning/project.json")
        self.episode = core.read_json(p.ROOT / "examples/coffee-morning/shots.json")
        self.shot = self.episode["shots"][0]
        self.shot["references"] = []
        self.shot["audio"] = {}
        self.shot["planned_edit_seconds"] = 1
        self.state = core.load_state(self.directory, self.project)
        self.write_inputs()

    def tearDown(self):
        self.temporary.cleanup()

    def write_inputs(self):
        p.write_json(self.directory / "project.json", self.project)
        p.write_json(self.directory / "shots.json", self.episode)

    def add_shot(self, number):
        shot = copy.deepcopy(self.shot)
        shot["shot_id"] = "E01-SC01-SH{:03}".format(number)
        self.episode["shots"].append(shot)
        self.episode["edit_order"].append(core.shot_key(shot))
        return shot


class ProductionTests(ProductionFixture):
    def test_storyboard_exports_timed_subtitles_and_safe_csv(self):
        second = self.add_shot(2)
        self.shot["planned_edit_seconds"] = 1.5
        self.shot["narrative_goal"] = "=HYPERLINK(unsafe)"
        second["subtitle"] = "第二镜"
        result = p.storyboard(self.directory, self.project, self.episode)
        folder = Path(result["directory"])
        with (folder / "storyboard.csv").open(encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["narrative_goal"], "'=HYPERLINK(unsafe)")
        text = (folder / "subtitles.srt").read_text()
        self.assertIn("00:00:01,500 --> 00:00:02,500", text)
        self.assertEqual(result["duration"], 2.5)
        self.assertIn("第二镜", text)
        del self.shot["action"]
        with self.assertRaisesRegex(p.Error, "action"):
            p.storyboard(self.directory, self.project, self.episode)

    def test_revisions_do_not_replace_selected_or_edit_order(self):
        key = core.shot_key(self.shot)
        self.state["selected"][self.shot["shot_id"]] = {"revision": "v01"}
        p.revise(self.directory, self.episode, self.shot, "v02")
        self.assertEqual(self.episode["edit_order"], [key])
        self.assertEqual(self.state["selected"][self.shot["shot_id"]]["revision"], "v01")
        self.episode.pop("edit_order")
        with self.assertRaisesRegex(p.Error, "one revision"):
            p.plan_shots(self.episode)
        with self.assertRaisesRegex(p.Error, "already exists"):
            p.revise(self.directory, self.episode, self.shot, "v02")

    def test_invalid_timing_rejected(self):
        for value in (True, -1, float("nan"), float("inf"), "bad"):
            with self.subTest(value=value), self.assertRaises(p.Error):
                p.finite(value, "timing")

    def test_upload_limits_use_actual_duration_and_detect_duplicates(self):
        def info(file, *args, **kwargs):
            return {"duration": float(file), "size": 100, "sha256": str(file)}
        with patch("production.inspect_media", side_effect=info):
            result = p.audit(["600", "599.999"], "comic")
            self.assertFalse(result["passed_media_limits"])
            self.assertTrue(any("1200" in x for x in result["problems"]))
            self.assertFalse(p.audit(["600", "600"], "comic")["passed_media_limits"])
            self.assertTrue(p.audit(["600", "300", "301"], "comic")["passed_media_limits"])
            self.assertTrue(p.audit(["180"], "live")["passed_media_limits"])
            self.assertFalse(p.audit(["180.01"], "live")["passed_media_limits"])
            self.assertFalse(p.audit(["1200"], "comic", high_budget=True)["passed_media_limits"])

    def test_atomic_document_write_preserves_original_on_failure(self):
        path = self.directory / "shots.json"
        before = path.read_bytes()
        with patch("production.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                p.write_json(path, {"new": True})
        self.assertEqual(path.read_bytes(), before)


class MediaIntegrationTests(ProductionFixture):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ffmpeg = p.tool("ffmpeg")
            p.tool("ffprobe")
        except p.Error as exc:
            raise unittest.SkipTest(str(exc))

    def clip(self, name, color="red", sound=False, size="96x64", rate=15):
        file = self.directory / name
        command = [self.ffmpeg, "-nostdin", "-v", "error", "-n", "-f", "lavfi", "-i",
                   "color=c={}:s={}:r={}:d=2".format(color, size, rate)]
        if sound:
            command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
        command += ["-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        if sound:
            command += ["-c:a", "aac"]
        p.execute(command + [str(file)])
        return file

    def image(self):
        path = self.directory / "test image.png"
        p.execute([self.ffmpeg, "-nostdin", "-v", "error", "-n", "-f", "lavfi", "-i",
                   "color=c=red:s=96x64", "-frames:v", "1", "-update", "1", str(path)])
        return path

    def test_import_checks_real_media_and_preserves_extra_references(self):
        image = self.image()
        self.shot["references"] = [{"file": None}, {"resource_id": "keep-other-reference"}]
        record = p.import_media(self.directory, self.episode, self.state, self.shot, image, "image", "fixture")
        self.assertEqual(self.shot["references"][1]["resource_id"], "keep-other-reference")
        self.assertEqual(record["sha256"], core.sha256(image))
        with self.assertRaisesRegex(p.Error, "Expected video"):
            p.import_media(self.directory, self.episode, self.state, self.shot, image, "video", "fixture")
        fake = self.directory / "fake.mp4"
        fake.write_bytes(b"not a real video")
        with self.assertRaises(p.Error):
            p.inspect_media(fake, "video", decode=True)
        Path(record["file"]).write_bytes(b"changed")
        with self.assertRaisesRegex(p.Error, "changed"):
            p.reference_image(self.directory, self.state, self.shot)

    def test_import_select_checks_input_and_file_without_remote_calls(self):
        file = self.clip("input.mp4")
        with patch("workflow.call_cli", side_effect=AssertionError("No network allowed")):
            record = p.import_media(self.directory, self.episode, self.state, self.shot, file, "video", "manual fixture")
            self.assertEqual(record["external_cost"], "unknown")
            self.assertNotIn("resource_id", record)
            selected = p.select(self.directory, self.state, self.shot, "Technical test selection")
            self.assertEqual(selected["sha256"], record["sha256"])
            self.shot["video_request"]["prompt"] += " changed"
            with self.assertRaisesRegex(p.Error, "inputs changed"):
                p.select(self.directory, self.state, self.shot, "no")
            self.shot["video_request"]["prompt"] = record["input"]["video_request"]["prompt"]
            Path(record["file"]).write_bytes(b"changed")
            with self.assertRaisesRegex(p.Error, "changed"):
                p.select(self.directory, self.state, self.shot, "no")

    def test_cut_uses_selected_order_and_normalizes_media_and_audio(self):
        second = self.add_shot(2)
        first_file = self.clip("red video.mp4", sound=True)
        second_file = self.clip("blue video.mp4", color="blue", size="64x96", rate=24)
        for shot, file in ((self.shot, first_file), (second, second_file)):
            p.import_media(self.directory, self.episode, self.state, shot, file, "video", "synthetic test")
            p.select(self.directory, self.state, shot, "Synthetic test selection")
        self.episode["edit_order"].reverse()
        result = p.render(self.directory, self.project, self.episode, self.state, "rough-cut", "96x128", 24)
        info = result["video"]
        self.assertAlmostEqual(info["duration"], 2, delta=0.1)
        self.assertEqual((info["width"], info["height"]), (96, 128))
        self.assertEqual(info["audio_codec"], "aac")
        # Probe center pixels at both cuts to verify actual output order, not just a manifest.
        def pixel(at):
            return subprocess.check_output([self.ffmpeg, "-v", "error", "-ss", str(at), "-i", info["file"],
                "-vf", "crop=2:2:(iw-2)/2:(ih-2)/2,format=rgb24", "-frames:v", "1", "-f", "rawvideo", "-"])[:3]
        blue, red = pixel(0.5), pixel(1.5)
        self.assertGreater(blue[2], blue[0] + 100)
        self.assertGreater(red[0], red[2] + 100)
        packaged = p.package(self.directory, self.project, Path(result["directory"]))
        self.assertEqual(packaged["purpose"], "internal_review")
        (Path(result["directory"]) / "subtitles.srt").write_text("changed")
        with self.assertRaisesRegex(p.Error, "Subtitles changed"):
            p.package(self.directory, self.project, Path(result["directory"]))

    def test_missing_selection_short_video_and_long_narration_block_render(self):
        with self.assertRaisesRegex(p.Error, "select"):
            p.render(self.directory, self.project, self.episode, self.state, "rough-cut", "96x128", 24)
        file = self.clip("input.mp4")
        p.import_media(self.directory, self.episode, self.state, self.shot, file, "video", "test")
        p.select(self.directory, self.state, self.shot, "test")
        self.shot["planned_edit_seconds"] = 3
        with self.assertRaisesRegex(p.Error, "shorter"):
            p.render(self.directory, self.project, self.episode, self.state, "rough-cut", "96x128", 24)
        self.shot["planned_edit_seconds"] = 1
        audio = self.directory / "long.wav"
        p.execute([self.ffmpeg, "-v", "error", "-n", "-f", "lavfi", "-i", "sine=duration=2", str(audio)])
        self.shot["audio"] = {"file": str(audio)}
        with self.assertRaisesRegex(p.Error, "narration exceeds"):
            p.render(self.directory, self.project, self.episode, self.state, "rough-cut", "96x128", 24)

    def test_static_preview_and_music_do_not_require_video_generation(self):
        image = self.image()
        p.import_media(self.directory, self.episode, self.state, self.shot, image, "image", "test")
        music = self.directory / "music.wav"
        p.execute([self.ffmpeg, "-v", "error", "-n", "-f", "lavfi", "-i", "sine=duration=0.5", str(music)])
        with patch("workflow.call_cli", side_effect=AssertionError("No network allowed")):
            result = p.render(self.directory, self.project, self.episode, self.state, "preview", "96x128", 24, music)
        self.assertEqual(result["mode"], "preview")
        self.assertAlmostEqual(result["video"]["duration"], 1, delta=0.1)
        manifest = core.read_json(Path(result["directory"]) / "render.json")
        self.assertTrue(manifest["sources"][0]["still"])
        self.assertEqual(self.state["operations"], {})

    def test_visible_subtitles_and_narration_gain_in_actual_output(self):
        if not p.re.search(r"\bsubtitles\s+V->V", p.execute([self.ffmpeg, "-hide_banner", "-filters"])):
            self.skipTest("FFmpeg lacks libass subtitle rendering")
        image = self.image()
        p.import_media(self.directory, self.episode, self.state, self.shot, image, "image", "test")
        sound = self.directory / "voice.wav"
        p.execute([self.ffmpeg, "-v", "error", "-n", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.7", "-ac", "2", str(sound)])
        p.import_media(self.directory, self.episode, self.state, self.shot, sound, "audio", "test tone")
        result = p.render(self.directory, self.project, self.episode, self.state, "preview", "192x320", 24,
                          burn_subtitles=True, narration_gain_db=4)
        manifest = core.read_json(Path(result["directory"]) / "render.json")
        self.assertTrue(manifest["subtitles_burned"])
        rgb = subprocess.check_output([self.ffmpeg, "-v", "error", "-ss", "0.3", "-i", result["video"]["file"],
            "-vf", "format=rgb24", "-frames:v", "1", "-f", "rawvideo", "-"])
        # A red test image has no white pixels: visible light text must be rendered into the frame.
        pixels = zip(rgb[0::3], rgb[1::3], rgb[2::3])
        self.assertGreater(sum(min(pixel) > 180 for pixel in pixels), 10)
        def rms(path):
            samples = array.array("h", subprocess.check_output([self.ffmpeg, "-v", "error", "-ss", "0.1", "-i", str(path),
                "-t", "0.3", "-vn", "-ac", "1", "-ar", "48000", "-f", "s16le", "-"]))
            return math.sqrt(sum(sample * sample for sample in samples) / len(samples))
        gain = 20 * math.log10(rms(result["video"]["file"]) / rms(sound))
        self.assertAlmostEqual(gain, 4, delta=0.5)


if __name__ == "__main__":
    unittest.main()
