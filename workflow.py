#!/usr/bin/env python3
"""Single-shot workflow for the dreamina-canvas 1.0.2, kling-cli 0.2.3 and dreamina 1.4.21 command contracts.

project.json selects the platform with "generator": "dreamina" (canvas, default), "kling" or "jimeng"
(the 即梦 dreamina CLI).
"""

import argparse
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from datetime import datetime, timezone


class WorkflowError(Exception):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowError("Cannot read {}: {}".format(path, exc))
    if not isinstance(value, dict):
        raise WorkflowError("{} must contain a JSON object".format(path))
    return value


def credits(value):
    if value is None or isinstance(value, bool):
        raise WorkflowError("Credit amount is unknown or invalid")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise WorkflowError("Invalid credit amount: {!r}".format(value))
    if not result.is_finite() or result < 0:
        raise WorkflowError("Credit amount must be finite and non-negative")
    return result


def identifier(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise WorkflowError("{} must be a nonempty ID using letters, digits, _ or -".format(field))


def load_inputs(directory):
    project = read_json(directory / "project.json")
    episode = read_json(directory / "shots.json")
    if project.get("schema_version") != "0.2" or episode.get("schema_version") != "0.2":
        raise WorkflowError("Input schema_version must be 0.2")
    identifier(project.get("project_id"), "project_id")
    if episode.get("project_id") != project["project_id"]:
        raise WorkflowError("project.json and shots.json must use the same project_id")
    shots = episode.get("shots")
    if not isinstance(shots, list) or not shots:
        raise WorkflowError("shots.json must contain a nonempty shots list")
    seen = set()
    for shot in shots:
        if not isinstance(shot, dict):
            raise WorkflowError("Each shot must be an object")
        identifier(shot.get("shot_id"), "shot_id")
        identifier(shot.get("revision"), "revision")
        key = shot["shot_id"] + "@" + shot["revision"]
        if key in seen:
            raise WorkflowError("Duplicate shot version: " + key)
        seen.add(key)
        if not isinstance(shot.get("video_request"), dict):
            raise WorkflowError(key + " requires video_request")
        if not isinstance(shot.get("references"), list):
            raise WorkflowError(key + " requires a references list")
        duration = shot.get("planned_edit_seconds")
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or duration <= 0):
            raise WorkflowError(key + " requires a positive planned_edit_seconds")
    authorization = project.get("generation_authorization")
    if not isinstance(authorization, dict) or not isinstance(authorization.get("approved"), bool):
        raise WorkflowError("generation_authorization.approved must be a boolean")
    if authorization.get("credit_cap") is not None:
        credits(authorization["credit_cap"])
    return project, episode


def load_state(directory, project):
    path = directory / "state.json"
    if not path.exists():
        return {"schema_version": "1", "project_id": project["project_id"], "operations": {}, "selected": {}}
    state = read_json(path)
    if state.get("schema_version") != "1" or state.get("project_id") != project["project_id"]:
        raise WorkflowError("State schema or project_id does not match")
    if not isinstance(state.get("operations"), dict) or not isinstance(state.get("selected"), dict):
        raise WorkflowError("State requires operations and selected objects")
    return state


def write_state(directory, state):
    state["updated_at"] = timestamp()
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=str(directory))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(directory / "state.json"))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def project_lock(directory):
    with (directory / ".workflow.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkflowError("Another workflow process is using this project")
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def choose_shot(episode, shot_id=None, revision=None):
    matches = [shot for shot in episode["shots"] if
               (shot_id is None or shot["shot_id"] == shot_id) and
               (revision is None or shot["revision"] == revision)]
    if len(matches) != 1:
        raise WorkflowError("Specify --shot and --revision to select exactly one input version")
    return matches[0]


def shot_key(shot):
    return shot["shot_id"] + "@" + shot["revision"]


def check_budget(project, state, shot, quoted):
    authorization = project["generation_authorization"]
    scope = authorization.get("scope")
    if (authorization.get("approved") is not True or not authorization.get("approval_ref")
            or not isinstance(scope, list) or not ("*" in scope or shot_key(shot) in scope)):
        raise WorkflowError("No explicit generation authorization covers this shot version")
    if project.get("example_only"):
        raise WorkflowError("Example inputs cannot submit generation; prepare a real project")
    cap = credits(authorization.get("credit_cap"))
    amount = credits(quoted)
    occupied = Decimal("0")
    for record in state["operations"].values():
        if not isinstance(record, dict):
            raise WorkflowError("Invalid operation record")
        actual = record.get("actual_credits")
        reserved = record.get("reserved_credits")
        if actual is not None:
            occupied += credits(actual)
        elif reserved is not None:
            occupied += credits(reserved)
        elif record.get("phase") in ("submitting", "submitted", "ready", "downloaded", "needs_check", "failed"):
            raise WorkflowError("An earlier operation has unknown credit usage; reconcile it first")
    if occupied + amount > cap:
        raise WorkflowError("Credit cap exceeded: {} occupied + {} quoted > {}".format(occupied, amount, cap))
    return amount


def cli_path():
    bundled = Path(__file__).resolve().parent / ".tools/bin/dreamina-canvas"
    found = str(bundled) if bundled.is_file() else shutil.which("dreamina-canvas")
    if not found:
        raise WorkflowError("dreamina-canvas is not installed; no legacy fallback is allowed")
    return found


def call_cli(directory, arguments):
    environment = os.environ.copy()
    journal = directory / ".cli-state"
    journal.mkdir(mode=0o700, exist_ok=True)
    environment["DREAMINA_CANVAS_STATE_DIR"] = str(journal.resolve())
    try:
        result = subprocess.run([cli_path(), "--format", "json", "--non-interactive"] + arguments,
            capture_output=True, text=True, env=environment, timeout=60)
        # This release writes JSON failure envelopes to stderr.
        payload = json.loads(result.stdout.strip() or result.stderr.strip())
    except (subprocess.TimeoutExpired, ValueError) as exc:
        raise WorkflowError("CLI response unavailable; inspect the original IDs before retrying: " + type(exc).__name__)
    if not isinstance(payload, dict) or payload.get("schemaVersion") != "1":
        raise WorkflowError("Unrecognized CLI response schema")
    if result.returncode != 0 or payload.get("ok") is not True:
        error = payload.get("error") or {}
        # Do not serialize tokens, signed URLs, raw provider payloads, or raw stderr.
        raise WorkflowError("CLI {}: {} (reason: {}, action: {}, request: {})".format(result.returncode,
            error.get("code", "unknown"), error.get("reason", "unknown"), error.get("requiredAction", "inspect"),
            (payload.get("meta") or {}).get("requestId") or error.get("requestId", "unknown")))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise WorkflowError("CLI did not return an object in data")
    return data


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def public_uuid(value, field):
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise WorkflowError(field + " must be a canonical lowercase UUID")
    if canonical != value:
        raise WorkflowError(field + " must be a canonical lowercase UUID")
    return value


def new_node_id():
    # Format verified by the installed CLI's local validation.
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    return "node_" + "".join(secrets.choice(alphabet) for _ in range(10))


def generation(node):
    value = node.get("generation")
    if not isinstance(value, dict):
        raise WorkflowError("Node has no readable generation settings")
    result = {key: value.get(key) for key in
        ("mode", "prompt", "model", "ratio", "resolution", "durationSeconds", "outputCount")}
    result["references"] = [{"kind": ref.get("kind"), "id": ref.get("id")}
                            for ref in value.get("references", [])]
    return result


def read_node(directory, record, media_type="video"):
    data = call_cli(directory, ["node", "show", "--project-id", record["project_id"],
                               "--node-id", record["node_id"]])
    items = data.get("nodes", [])
    if len(items) != 1 or items[0].get("result") != "FOUND" or not isinstance(items[0].get("node"), dict):
        raise WorkflowError("Saved node cannot be authoritatively read")
    node = items[0]["node"]
    if node.get("nodeId") != record["node_id"] or node.get("type") != media_type:
        raise WorkflowError("Read-back node identity/type differs from the requested media")
    return node


def snapshot(directory, shot):
    value = json.loads(json.dumps(shot))
    for ref in value["references"]:
        if not isinstance(ref, dict):
            raise WorkflowError("Each reference must be an object")
        if ref.get("file") and ref.get("resource_id"):
            raise WorkflowError("Choose either file or resource_id for each reference, not both")
        if ref.get("file"):
            path = (directory / ref["file"]).resolve()
            if not path.is_file() or path.stat().st_size == 0:
                raise WorkflowError("Reference file is missing or empty: " + str(path))
            ref["file"] = str(path)
            ref["sha256"] = sha256(path)
        if ref.get("resource_id"):
            public_uuid(ref["resource_id"], "reference.resource_id")
        if not ref.get("file") and not ref.get("resource_id"):
            raise WorkflowError("Select a real reference file or resource_id first")
    return value


def current_record(directory, state, shot):
    record = state["operations"].get(shot_key(shot))
    if not isinstance(record, dict):
        raise WorkflowError("Save this shot version first")
    if record.get("input") != snapshot(directory, shot):
        raise WorkflowError("Inputs changed; use a new revision instead of reusing this operation")
    return record


def ensure_logged_in(directory):
    data = call_cli(directory, ["auth", "status"])
    if not data.get("loggedIn"):
        raise WorkflowError("CLI cannot read a login in this process; authorize or use a normal terminal")
    if data.get("region") != "cn" or data.get("environment") != "prod" or data.get("profile") != "default":
        raise WorkflowError("This MVP supports the verified default/cn/prod context only")


def save_shot(directory, project, state, shot):
    if project.get("example_only"):
        raise WorkflowError("Example inputs cannot modify the platform; prepare a real project")
    ensure_logged_in(directory)
    project_id = public_uuid(project.get("canvas_project_id"), "canvas_project_id")
    inputs = snapshot(directory, shot)
    request = inputs["video_request"]
    if request.get("mode") not in ("m2v", "first_last_frame", "t2v"):
        raise WorkflowError("Supported video modes: m2v, first_last_frame, t2v")
    if request["mode"] == "t2v" and inputs["references"]:
        raise WorkflowError("t2v cannot have references")
    if request["mode"] != "t2v" and not inputs["references"]:
        raise WorkflowError("Choose at least one actual image reference")
    if request["mode"] == "first_last_frame" and len(inputs["references"]) not in (1, 2):
        raise WorkflowError("first_last_frame requires one or two ordered image references")
    extra = set(request) - {"mode", "prompt", "model", "duration", "ratio", "resolution"}
    if extra:
        raise WorkflowError("Unsupported video_request fields: " + ", ".join(sorted(extra)))
    for field in ("prompt", "model", "duration", "ratio", "resolution"):
        if request.get(field) in (None, ""):
            raise WorkflowError("Choose video_request." + field + " using the current model catalogue")
    if (isinstance(request["duration"], bool) or not isinstance(request["duration"], (int, float))
            or not math.isfinite(request["duration"]) or request["duration"] <= 0):
        raise WorkflowError("video_request.duration must be a positive finite number")
    key = shot_key(shot)
    record = state["operations"].get(key)
    if record:
        if record.get("project_id") != project_id:
            raise WorkflowError("This version belongs to another canvas; do not reuse its save IDs")
        current_record(directory, state, shot)
        if record["phase"] != "saving":
            return record
        # Pure-save recovery reads first, then reuses exactly the frozen save IDs.
        observed = call_cli(directory, ["node", "show", "--project-id", project_id,
                                       "--node-id", record["node_id"]])
        for item in observed.get("nodes", []):
            if item.get("nodeId") == record["node_id"] and item.get("result") == "FOUND":
                node = item.get("node") or {}
                expected = {"mode": request["mode"], "prompt": request["prompt"], "model": request["model"],
                    "ratio": request["ratio"], "resolution": request["resolution"],
                    "durationSeconds": request["duration"], "outputCount": 1,
                    "references": [{"kind": "node", "id": ref["node_id"]} for ref in record["references"]]}
                if generation(node) != expected:
                    raise WorkflowError("A saved node exists with different settings; inspect before retrying")
                record.update(phase="saved", generation=generation(node))
                write_state(directory, state)
                return record
    else:
        record = {"input": inputs, "project_id": project_id, "node_id": new_node_id(),
            "update_id": str(uuid.uuid4()), "phase": "saving", "references": [],
            "quote_credits": None, "reserved_credits": None, "actual_credits": None}
        # Freeze upload/import and node-save identities before any network mutation.
        for ref in inputs["references"]:
            record["references"].append({"resource_id": ref.get("resource_id") or str(uuid.uuid4()),
                "uploaded": not bool(ref.get("file")), "file": ref.get("file"),
                "node_id": new_node_id(), "update_id": str(uuid.uuid4()),
                "import_submit_id": str(uuid.uuid4()), "imported": False})
        state["operations"][key] = record
        write_state(directory, state)
    for ref in record["references"]:
        if not ref["uploaded"]:
            data = call_cli(directory, ["resource", "upload", "--project-id", project_id,
                "--resource-id", ref["resource_id"], "--file", ref["file"], "--type", "image"])
            if data.get("resourceId") != ref["resource_id"] or data.get("mediaType") != "image":
                raise WorkflowError("Upload identity/type could not be confirmed")
            ref["uploaded"] = True
            write_state(directory, state)
        if not ref["imported"]:
            data = call_cli(directory, ["node", "create", "image", "--project-id", project_id,
                "--node-id", ref["node_id"], "--update-id", ref["update_id"],
                "--submit-id", ref["import_submit_id"], "--resource-id", ref["resource_id"],
                "--import-kind", "local_upload", "--title", key + " reference"])
            node = data.get("node") or read_node(directory, {"project_id": project_id, "node_id": ref["node_id"]}, "image")
            if node.get("nodeId") != ref["node_id"]:
                raise WorkflowError("Imported reference node identity could not be confirmed")
            ref["imported"] = True
            write_state(directory, state)
    args = ["node", "create", "video", "--project-id", project_id,
        "--node-id", record["node_id"], "--update-id", record["update_id"], "--title", key]
    for field in ("mode", "prompt", "model", "duration", "ratio", "resolution"):
        args.extend(["--" + field, str(request[field])])
    args.extend(["--count", "1"])
    for ref in record["references"]:
        args.extend(["--ref", "node:" + ref["node_id"]])
    data = call_cli(directory, args)
    node = data.get("node") or read_node(directory, record)
    if node.get("nodeId") != record["node_id"]:
        raise WorkflowError("Video node identity could not be confirmed")
    record["generation"] = generation(node)
    expected = {ref["node_id"] for ref in record["references"]}
    observed = {ref["id"] for ref in record["generation"]["references"] if ref["kind"] == "node"}
    if expected != observed or not expected.issubset(set(node.get("upstreamNodeIds", []))):
        raise WorkflowError("Saved reference identities/connections differ from the planned references")
    if record["generation"].get("outputCount") != 1:
        raise WorkflowError("The saved node must produce exactly one video")
    record["phase"] = "saved"
    write_state(directory, state)
    return record


def quote_shot(directory, state, shot):
    record = current_record(directory, state, shot)
    if record["phase"] not in ("saved", "quoted"):
        raise WorkflowError("Already submitted or uncertain; use resume, never quote-and-run again")
    if generation(read_node(directory, record)) != record["generation"]:
        raise WorkflowError("Canvas settings changed; review and save a new shot revision")
    data = call_cli(directory, ["node", "quote", "--project-id", record["project_id"],
                              "--node-id", record["node_id"]])
    items = data.get("items", [])
    if len(items) != 1 or items[0].get("nodeId") != record["node_id"] or items[0].get("error"):
        raise WorkflowError("Node cannot be quoted")
    amount = credits(items[0].get("maxCredits"))
    if amount != credits(data.get("totalMaxCredits")) or amount != amount.to_integral_value():
        raise WorkflowError("Quote is not a complete single-node integer-credit total")
    record.update(phase="quoted", quote_credits=str(amount), quoted_at=timestamp())
    write_state(directory, state)
    return record


def run_shot(directory, project, state, shot):
    record = current_record(directory, state, shot)
    if record.get("project_id") != project.get("canvas_project_id"):
        raise WorkflowError("Current project canvas differs from this saved operation")
    if record["phase"] not in ("saved", "quoted"):
        raise WorkflowError("This version was submitted or is uncertain; use resume or a new revision")
    # Check existing authorization before even issuing a confirmation request.
    check_budget(project, state, shot, record.get("quote_credits"))
    record = quote_shot(directory, state, shot)
    ceiling = check_budget(project, state, shot, record["quote_credits"])
    approval = call_cli(directory, ["node", "confirm", "--project-id", record["project_id"],
        "--node-id", record["node_id"], "--credit-ceiling", str(int(ceiling))])
    token = approval.get("creditConfirmationToken")
    if not isinstance(token, str) or not token or credits(approval.get("creditCeiling")) != ceiling:
        raise WorkflowError("No valid confirmation for the exact credit ceiling")
    record.update(phase="submitting", submit_id=str(uuid.uuid4()), reserved_credits=str(ceiling),
                  authorization_ref=project["generation_authorization"]["approval_ref"])
    write_state(directory, state)
    try:
        data = call_cli(directory, ["node", "run", "--project-id", record["project_id"],
            "--node-id", record["node_id"], "--submit-id", record["submit_id"], "--credit-token", token])
        items = data.get("items", [])
        if (len(items) != 1 or items[0].get("nodeId") != record["node_id"]
                or items[0].get("submitId") != record["submit_id"] or str(items[0].get("state", "")).lower() != "accepted"):
            raise WorkflowError("Submission acceptance is not confirmed; inspect original submit_id")
        record["phase"] = "submitted"
    except WorkflowError:
        record["phase"] = "needs_check"
        write_state(directory, state)
        raise
    write_state(directory, state)
    return record


def resume_shot(directory, state, shot):
    # Recovery remains available when inputs changed: never create a new submission.
    record = state["operations"].get(shot_key(shot))
    if not isinstance(record, dict) or not record.get("submit_id"):
        raise WorkflowError("No original submit_id; inspect the canvas before taking further action")
    if record.get("phase") == "downloaded":
        path = Path(record.get("file") or "")
        if path.is_file() and sha256(path) == record.get("sha256"):
            return record
    data = call_cli(directory, ["operation", "status", record["submit_id"],
                               "--project-id", record["project_id"]])
    if data.get("operationRef") != record["submit_id"]:
        raise WorkflowError("Operation identity differs from the original submission")
    resources = data.get("resources") or []
    record["operation_state"] = data.get("state")
    record["submission_state"] = (data.get("submission") or {}).get("state")
    operation_state = str(data.get("state", "")).lower()
    if operation_state != "succeeded" or len(resources) != 1 or str(resources[0].get("state", "")).lower() != "succeeded":
        record["phase"] = "failed" if operation_state == "failed" else "needs_check" if operation_state not in ("running", "pending") else "submitted"
        write_state(directory, state)
        return record
    resource_id = public_uuid(resources[0].get("resourceId"), "resource_id")
    resource = call_cli(directory, ["resource", "get", resource_id, "--project-id", record["project_id"]])
    if (resource.get("resourceId") != resource_id or resource.get("type") != "video"
            or str(resource.get("status", "")).lower() != "succeeded"):
        raise WorkflowError("Resource identity/type/success state could not be confirmed")
    record.update(phase="ready", resource_id=resource_id)
    write_state(directory, state)
    output_dir = directory / "outputs" / shot_key(shot)
    output_dir.mkdir(parents=True, exist_ok=True)
    extension = resources[0].get("format", "").lower()
    if extension not in ("mp4", "mov", "webm", "mkv", "avi"):
        extension = "video"
    destination = output_dir / (resource_id + "." + extension)
    if destination.exists():
        # Preserve an unverified/interrupted download; never overwrite a previous file.
        destination = output_dir / (resource_id + "-" + uuid.uuid4().hex[:8] + "." + extension)
    download = call_cli(directory, ["resource", "download", resource_id,
        "--project-id", record["project_id"], "--output", str(destination)])
    if download.get("resourceId") != resource_id or Path(download.get("path") or "").resolve() != destination.resolve():
        raise WorkflowError("Download identity/path could not be confirmed")
    if not destination.is_file() or destination.stat().st_size <= 0 or destination.stat().st_size != download.get("size"):
        raise WorkflowError("Downloaded file is missing, empty, or has an unexpected size")
    checksum = sha256(destination)
    if checksum != download.get("sha256"):
        raise WorkflowError("Downloaded file checksum differs from the CLI result")
    record.update(phase="downloaded", file=str(destination.resolve()), sha256=checksum,
                  size=destination.stat().st_size, playback_review="pending")
    write_state(directory, state)
    return record


KLING_SLOTS = ["first_image", "tail_image"] + ["keyframe_{}".format(n) for n in range(1, 11)]
KLING_PENDING = {"submitted", "pending", "queuing", "queueing", "processing", "running"}
KLING_SUCCESS = {"succeed", "succeeded", "success", "completed"}


def kling_path():
    bundled = Path(__file__).resolve().parent / ".tools/kling/node_modules/.bin/kling"
    found = str(bundled) if bundled.is_file() else shutil.which("kling")
    if not found:
        raise WorkflowError("kling CLI is not installed; see .tools/kling/README.md")
    return found


def call_kling(arguments, timeout=60):
    environment = os.environ.copy()
    environment["KLING_NONINTERACTIVE"] = "1"
    try:
        result = subprocess.run([kling_path()] + arguments + ["--quiet"], capture_output=True, text=True,
                                env=environment, timeout=timeout)
        payload = json.loads(result.stdout.strip())
    except subprocess.TimeoutExpired as exc:
        raise WorkflowError("Kling CLI response unavailable: " + type(exc).__name__)
    except ValueError as exc:
        # Local CLI failures print plain text to stderr and nothing to stdout.
        detail = re.sub(r"https?://\S+", "<url>", (result.stderr or "").strip())[-400:]
        raise WorkflowError("Kling CLI response unavailable: {} (exit {}; stderr: {})".format(
            type(exc).__name__, result.returncode, detail or "empty"))
    if not isinstance(payload, dict) or not isinstance(payload.get("body"), dict):
        raise WorkflowError("Unrecognized Kling CLI response")
    if result.returncode != 0 or payload.get("ok") is not True:
        body = payload["body"]
        detail = body.get("error") or body.get("message") or body.get("code") or "unknown"
        # Signed result URLs and tokens must not reach errors or state.
        detail = re.sub(r"https?://\S+", "<url>", json.dumps(detail, ensure_ascii=False))[:300]
        raise WorkflowError("Kling CLI {} (status {}): {}".format(result.returncode, payload.get("status"), detail))
    return payload["body"]


KLING_TOOLS = {"kling_i2v": "image_to_video", "kling_omni": "omni_ref_video"}
KLING_FIELDS = {"kling_i2v": {"mode", "prompt", "model", "duration", "resolution"},
                "kling_omni": {"mode", "prompt", "model", "duration", "resolution", "aspect_ratio", "enable_audio",
                               "elements"}}
KLING_ELEMENT_TAGS = {"角色", "动物", "道具", "服饰", "场景", "特效", "其他"}
KLING_PROMPT_LIMIT = 8000  # characters; the service allows 8000 tokens, so this is conservative


def image_size(path):
    with path.open("rb") as stream:
        head = stream.read(64 * 1024)
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    if head.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(head):
            if head[index] != 0xFF:
                break
            marker, length = head[index + 1], int.from_bytes(head[index + 2:index + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2):
                return int.from_bytes(head[index + 7:index + 9], "big"), int.from_bytes(head[index + 5:index + 7], "big")
            index += 2 + length
    raise WorkflowError("Kling images must be PNG or JPG: " + str(path))


def kling_image_check(path):
    # Declared limits: PNG/JPG, at most 30MB, below 4K, aspect ratio 2:5 to 5:2.
    width, height = image_size(path)
    if path.stat().st_size > 30 * 1024 * 1024 or max(width, height) > 3840 or not 0.4 <= width / height <= 2.5:
        raise WorkflowError("Kling image exceeds 30MB/4K or the 2:5–5:2 aspect range: " + str(path))


def media_seconds(path):
    probe = shutil.which("ffprobe")
    if not probe:
        raise WorkflowError("ffprobe is required to bill reference video duration")
    result = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                            capture_output=True, text=True, timeout=60)
    try:
        seconds = float(result.stdout.strip())
    except ValueError:
        raise WorkflowError("Cannot read reference media duration: " + str(path))
    if result.returncode != 0 or not math.isfinite(seconds) or seconds <= 0:
        raise WorkflowError("Cannot read reference media duration: " + str(path))
    return seconds


def kling_request(directory, shot):
    inputs = snapshot(directory, shot)
    request = inputs["video_request"]
    mode = request.get("mode")
    if mode not in KLING_TOOLS:
        raise WorkflowError("Kling shots use video_request.mode kling_i2v or kling_omni")
    extra = set(request) - KLING_FIELDS[mode]
    if extra:
        raise WorkflowError("Unsupported Kling video_request fields: " + ", ".join(sorted(extra)))
    for field in ("prompt", "model", "resolution"):
        if not isinstance(request.get(field), str) or not request[field].strip():
            raise WorkflowError("Choose video_request." + field + " from `kling who_am_i`")
    if request["prompt"].startswith("--"):
        raise WorkflowError("Kling prompts cannot start with --; the CLI would read them as a flag")
    if len(request["prompt"]) > KLING_PROMPT_LIMIT:
        raise WorkflowError("Kling prompt is longer than {} characters".format(KLING_PROMPT_LIMIT))
    duration = request.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, int) or not 3 <= duration <= 30:
        raise WorkflowError("Kling video_request.duration must be an integer from 3 to 30 seconds")
    slots = [ref.get("slot") for ref in inputs["references"]]
    if any(ref.get("resource_id") or not ref.get("file") for ref in inputs["references"]):
        raise WorkflowError("Kling references must be local files")
    if len(slots) != len(set(slots)):
        raise WorkflowError("Each Kling input slot can be filled once")
    if mode == "kling_i2v":
        if slots.count("first_image") != 1 or len(slots) > 10 or any(slot not in KLING_SLOTS for slot in slots):
            raise WorkflowError("Kling references need one first_image, optional tail_image and keyframe_1..10, at most 10")
    else:
        if any(not isinstance(slot, str) or not re.fullmatch(r"(image|video|audio)_[1-9][0-9]?", slot) for slot in slots):
            raise WorkflowError("Kling omni references use image_N, video_N or audio_N slots")
        elements = request.get("elements", [])
        if not isinstance(elements, list) or len(elements) != len(set(elements)) or len(elements) > 7:
            raise WorkflowError("video_request.elements must list at most 7 distinct element keys")
        for key in elements:
            identifier(key, "element key")
        if set(re.findall(r"<<<([^<>]+)>>>", request["prompt"])) != set(elements):
            raise WorkflowError("Reference each element as <<<key>>> in the prompt, and only listed elements")
        if not slots and not elements:
            raise WorkflowError("Kling omni shots need at least one reference or element")
        if "enable_audio" in request and not isinstance(request["enable_audio"], bool):
            raise WorkflowError("video_request.enable_audio must be true or false")
        if "aspect_ratio" in request and not isinstance(request["aspect_ratio"], str):
            raise WorkflowError("video_request.aspect_ratio must be text")
    for ref in inputs["references"]:
        path = Path(ref["file"])
        if ref["slot"].startswith(("image", "first", "tail", "keyframe")):
            kling_image_check(path)
        elif ref["slot"].startswith("video") and path.stat().st_size > 300 * 1024 * 1024:
            raise WorkflowError("Kling reference video exceeds 300MB: " + str(path))
        elif ref["slot"].startswith("audio") and path.stat().st_size > 50 * 1024 * 1024:
            raise WorkflowError("Kling reference audio exceeds 50MB: " + str(path))
    return inputs


def kling_input_seconds(inputs):
    # Omni-Ref bills input video duration on top of the output duration.
    return sum(media_seconds(Path(ref["file"])) for ref in inputs["references"] if ref["slot"].startswith("video"))


def kling_estimate(project, request, input_seconds=0):
    rates = project["generation_authorization"].get("kling_credit_rate") or {}
    key = KLING_TOOLS[request["mode"]] + ":" + request["model"] + "@" + request["resolution"]
    if key not in rates:
        raise WorkflowError("Set generation_authorization.kling_credit_rate[{}] from the Kling price page".format(key))
    # Kling has no quote command: estimate from the authorized per-second rate, rounded up.
    amount = credits(rates[key]) * (Decimal(request["duration"]) + Decimal(str(input_seconds)))
    return amount.to_integral_value(rounding="ROUND_CEILING")


def kling_validate(declaration, request, inputs, bound):
    """Check the planned call against the live who_am_i declaration before any paid call."""
    tool = KLING_TOOLS[request["mode"]]
    models = ((declaration.get("availableModels") or {}).get(tool) or {}).get("models") or []
    matches = [model for model in models if model.get("model") == request["model"]]
    if len(matches) != 1:
        raise WorkflowError("who_am_i does not offer {} for {}".format(request["model"], tool))
    model = matches[0]
    arguments = {item.get("name"): item for item in model.get("arguments") or []}
    planned = {"duration": str(request["duration"]), "resolution": request["resolution"]}
    if "aspect_ratio" in request:
        planned["aspect_ratio"] = request["aspect_ratio"]
    if "enable_audio" in request:
        planned["enable_audio"] = "true" if request["enable_audio"] else "false"
    for name, value in planned.items():
        if name not in arguments:
            raise WorkflowError("who_am_i does not declare {} for {}".format(name, request["model"]))
        allowed = arguments[name].get("allowedValues")
        if allowed and value not in allowed:
            raise WorkflowError("{}={} is not allowed now; who_am_i allows {}".format(name, value, ", ".join(allowed)))
    slots = [ref["slot"] for ref in inputs["references"]]
    declared = {item.get("name") for item in model.get("inputs") or []}
    if not set(slots) <= declared:
        raise WorkflowError("who_am_i does not declare input slot(s): " + ", ".join(sorted(set(slots) - declared)))
    if bound:
        support = model.get("elementSupport") or {}
        if "elements" not in arguments:
            raise WorkflowError(request["model"] + " does not accept elements")
        kinds = [item["type"] for item in bound]
        if (len(bound) > support.get("maxTotalElements", 0)
                or kinds.count("image") > (support.get("maxImageElements", 0) if support.get("imageElements") else 0)
                or kinds.count("video") > (support.get("maxVideoElements", 0) if support.get("videoElements") else 0)
                or any(kind not in ("image", "video") for kind in kinds)):
            raise WorkflowError("Element count or type exceeds who_am_i elementSupport")
    cap = model.get("combinedItemCap")
    if cap:
        count = sum(1 for slot in slots if slot.startswith(tuple(cap.get("inputPrefixes") or [])))
        if "elements" in (cap.get("arguments") or []):
            count += len(bound)
        if count > cap.get("maxItems", 0):
            raise WorkflowError("{} inputs exceed the combined cap of {}".format(count, cap.get("maxItems")))
    return model


def kling_arguments(record):
    request = record["input"]["video_request"]
    args = [KLING_TOOLS[request["mode"]], "--model", request["model"], "--duration", str(request["duration"]),
            "--resolution", request["resolution"]]
    if "aspect_ratio" in request:
        args.extend(["--aspect_ratio", request["aspect_ratio"]])
    if "enable_audio" in request:
        args.extend(["--enable_audio", "true" if request["enable_audio"] else "false"])
    for ref in record["input"]["references"]:
        args.extend(["--input", ref["slot"] + "=" + ref["file"]])
    prompt = request["prompt"]
    if record.get("bound_elements"):
        args.extend(["--elements", json.dumps([{key: item[key] for key in ("id", "bindName", "type")}
                                               for item in record["bound_elements"]], ensure_ascii=False)])
        for item in record["bound_elements"]:
            prompt = prompt.replace("<<<" + item["key"] + ">>>", "<<<" + item["id"] + ">>>")
    # kling-cli parses "--name=value" with a single-line pattern, so a multi-line prompt must be a separate argv.
    args.extend(["--prompt", prompt])
    return args


def load_elements(directory):
    path = directory / "elements.json"
    if not path.exists():
        return {}
    value = read_json(path)
    if value.get("schema_version") != "0.2" or not isinstance(value.get("elements"), list):
        raise WorkflowError("elements.json requires schema_version 0.2 and an elements list")
    result = {}
    for item in value["elements"]:
        if not isinstance(item, dict):
            raise WorkflowError("Each element must be an object")
        identifier(item.get("key"), "element key")
        if item["key"] in result:
            raise WorkflowError("Duplicate element key: " + item["key"])
        result[item["key"]] = item
    return result


def element_inputs(directory, definition):
    for field in ("name", "description"):
        if not isinstance(definition.get(field), str) or not definition[field].strip():
            raise WorkflowError("Element {} needs a {}".format(definition["key"], field))
    tags = definition.get("tags")
    if not isinstance(tags, list) or not tags or not set(tags) <= KLING_ELEMENT_TAGS:
        raise WorkflowError("Element tags must come from: " + ", ".join(sorted(KLING_ELEMENT_TAGS)))
    secondary = definition.get("secondary")
    if not isinstance(definition.get("cover"), str) or not isinstance(secondary, list) or not 1 <= len(secondary) <= 3:
        raise WorkflowError("Image elements need one cover and 1-3 secondary images")
    files = []
    for name in [definition["cover"]] + secondary:
        path = (directory / name).resolve()
        if not path.is_file():
            raise WorkflowError("Element image is missing: " + str(path))
        kling_image_check(path)
        files.append({"file": str(path), "sha256": sha256(path)})
    inputs = {"key": definition["key"], "name": definition["name"], "description": definition["description"],
              "tags": tags, "cover": files[0], "secondary": files[1:]}
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
    # The digest makes the remote name unique, so element_list can find an uncertain earlier creation.
    inputs["remote_name"] = "{} #{}".format(definition["name"], digest)
    return inputs


def kling_element(directory, project, state, key):
    authorization = project["generation_authorization"]
    if project.get("example_only") or authorization.get("approved") is not True or not authorization.get("approval_ref"):
        raise WorkflowError("Creating account elements needs a real project and explicit authorization")
    definitions = load_elements(directory)
    if key not in definitions:
        raise WorkflowError("elements.json has no element " + key)
    inputs = element_inputs(directory, definitions[key])
    elements = state.setdefault("elements", {})
    record = elements.get(key)
    if record and record.get("input") != inputs:
        raise WorkflowError("Element inputs changed; give the new version a new key")
    if record and record.get("phase") == "created":
        return record
    if not record:
        record = {"input": inputs, "phase": "creating", "started_at": timestamp()}
        elements[key] = record
        write_state(directory, state)
    found = [item for item in call_kling(["element_list"]).get("elements") or []
             if isinstance(item, dict) and item.get("name") == inputs["remote_name"]]
    if len(found) > 1:
        record["phase"] = "needs_check"
        write_state(directory, state)
        raise WorkflowError("Several Kling elements are named {}; resolve them in the app".format(inputs["remote_name"]))
    if found:
        element_id = str(found[0].get("id") or "")
    else:
        args = ["element_create", "--name", inputs["remote_name"], "--description", inputs["description"]]
        for tag in inputs["tags"]:
            args.extend(["--tag", tag])
        args.extend(["--cover", inputs["cover"]["file"]])
        for item in inputs["secondary"]:
            args.extend(["--secondary", item["file"]])
        try:
            body = call_kling(args, timeout=1800)
            created = body.get("element") if isinstance(body.get("element"), dict) else body
            element_id = str(created.get("id") or created.get("elementId") or created.get("element_id") or "")
            if not element_id:
                raise WorkflowError("element_create returned no id")
        except WorkflowError as exc:
            record["phase"] = "needs_check"
            write_state(directory, state)
            raise WorkflowError("Element creation is uncertain; rerun to look it up by name: " + str(exc))
    record.update(element_id=element_id, phase="listed")
    write_state(directory, state)
    detail = call_kling(["element_get", element_id])
    resource = detail.get("resource") if isinstance(detail.get("resource"), dict) else {}
    record.update(type="video" if resource.get("video") else "image", phase="created", created_at=timestamp())
    write_state(directory, state)
    return record


def kling_bound_elements(directory, state, request):
    bound = []
    definitions = load_elements(directory) if request.get("elements") else {}
    for key in request.get("elements", []):
        record = (state.get("elements") or {}).get(key)
        if key not in definitions or not record or record.get("phase") != "created":
            raise WorkflowError("Create element {} first with `workflow.py element --key {}`".format(key, key))
        if record["input"] != element_inputs(directory, definitions[key]):
            raise WorkflowError("Element {} changed after creation; give it a new key".format(key))
        bound.append({"key": key, "id": record["element_id"], "bindName": record["input"]["name"],
                      "type": record["type"]})
    return bound


def kling_save(directory, state, shot):
    inputs = kling_request(directory, shot)
    record = state["operations"].get(shot_key(shot))
    if record:
        return current_record(directory, state, shot)
    record = {"generator": "kling", "input": inputs, "phase": "saved",
              "quote_credits": None, "reserved_credits": None, "actual_credits": None}
    state["operations"][shot_key(shot)] = record
    write_state(directory, state)
    return record


def kling_available_credits():
    return credits(call_kling(["account"]).get("availableRemainCredits"))


def kling_quote(directory, project, state, shot):
    record = current_record(directory, state, shot)
    if record["phase"] not in ("saved", "quoted"):
        raise WorkflowError("Already submitted or uncertain; use resume, never quote-and-run again")
    amount = kling_estimate(project, record["input"]["video_request"], kling_input_seconds(record["input"]))
    record.update(phase="quoted", quote_credits=str(amount), account_credits=str(kling_available_credits()),
                  quoted_at=timestamp())
    write_state(directory, state)
    return record


def kling_run(directory, project, state, shot):
    record = current_record(directory, state, shot)
    if record["phase"] not in ("saved", "quoted"):
        raise WorkflowError("This version was submitted or is uncertain; use resume or a new revision")
    request = record["input"]["video_request"]
    # Authorization, elements and the cap are checked locally before any CLI call.
    bound = kling_bound_elements(directory, state, request)
    input_seconds = kling_input_seconds(record["input"])
    amount = check_budget(project, state, shot, kling_estimate(project, request, input_seconds))
    kling_validate(call_kling(["who_am_i"]), request, record["input"], bound)
    available = kling_available_credits()
    if available < amount:
        raise WorkflowError("Kling account has {} paid credits; estimate needs {}".format(available, amount))
    record.update(phase="submitting", submit_id=str(uuid.uuid4()), reserved_credits=str(amount),
                  quote_credits=str(amount), authorization_ref=project["generation_authorization"]["approval_ref"],
                  bound_elements=bound, input_seconds=input_seconds, submitted_at=timestamp())
    write_state(directory, state)
    args = kling_arguments(record)
    try:
        # Local frames are uploaded first; allow for the CLI's upload time budget.
        body = call_kling(args, timeout=1800)
        generation_id = body.get("generationId") or body.get("generation_id")
        if not isinstance(generation_id, str) or not generation_id:
            raise WorkflowError("Submission returned no generationId")
    except WorkflowError as exc:
        record["phase"] = "needs_check"
        write_state(directory, state)
        raise WorkflowError("Kling submission is uncertain; check the Kling app history, never resubmit this "
                            "version: " + str(exc))
    # Persist the accepted task before interpreting any other field of the response.
    record.update(phase="submitted", generation_id=generation_id)
    write_state(directory, state)
    consumed = body.get("creditsConsumed")
    try:
        record["actual_credits"] = str(credits(consumed)) if consumed is not None else None
    except WorkflowError:
        # Keep the reserved estimate occupying the cap until the charge is reconciled.
        record["credit_status"] = "needs_check"
    write_state(directory, state)
    return record


def kling_reconcile(directory, state, shot, evidence):
    """Return an uncertain submission to "saved" once the user confirmed it never reached the platform."""
    record = current_record(directory, state, shot)
    if (record.get("generator") not in ("kling", "jimeng") or record.get("phase") != "needs_check"
            or record.get("generation_id")):
        raise WorkflowError("Only a Kling/Jimeng needs_check record without a task id can be reconciled as not submitted")
    if not isinstance(evidence, str) or not evidence.strip():
        raise WorkflowError("Describe the evidence that nothing was submitted")
    record.setdefault("reconciliations", []).append({
        "at": timestamp(), "result": "not_submitted", "evidence": evidence.strip(),
        "submit_id": record.get("submit_id"), "reserved_credits": record.get("reserved_credits")})
    for field in ("submit_id", "submitted_at", "bound_elements", "input_seconds", "balance_before"):
        record.pop(field, None)
    record.update(phase="saved", reserved_credits=None, quote_credits=None)
    write_state(directory, state)
    return record


def kling_fetch(url, destination):
    """Download once; return the declared Content-Length (None when the server sends none)."""
    with urllib.request.urlopen(url, timeout=300) as response, destination.open("xb") as stream:
        declared = response.headers.get("Content-Length")
        shutil.copyfileobj(response, stream)
    if declared is None:
        return None
    if not declared.strip().isdigit():
        raise WorkflowError("Kling download has an invalid Content-Length")
    return int(declared)


def kling_resume(directory, state, shot):
    record = state["operations"].get(shot_key(shot))
    if not isinstance(record, dict) or not record.get("generation_id"):
        raise WorkflowError("No Kling generation_id; check the Kling app history before taking further action")
    if record.get("phase") == "downloaded":
        path = Path(record.get("file") or "")
        if path.is_file() and sha256(path) == record.get("sha256"):
            return record
    body = call_kling(["query_tasks", record["generation_id"]])
    status = str(body.get("status", "")).lower()
    record["task_status"] = status
    works = [work for work in body.get("works") or [] if isinstance(work, dict)]
    if status in KLING_PENDING:
        record["phase"] = "submitted"
    elif status not in KLING_SUCCESS:
        record["phase"] = "failed" if status == "failed" else "needs_check"
    elif len(works) != 1:
        record["phase"] = "needs_check"
    if status not in KLING_SUCCESS or len(works) != 1:
        write_state(directory, state)
        return record
    url = works[0].get("urlWithoutWatermark") or works[0].get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise WorkflowError("Kling result has no HTTPS download URL")
    output_dir = directory / "outputs" / shot_key(shot)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / (record["generation_id"] + ".mp4")
    if destination.exists():
        # Preserve an interrupted download; never overwrite a previous file.
        destination = output_dir / (record["generation_id"] + "-" + uuid.uuid4().hex[:8] + ".mp4")
    try:
        declared = kling_fetch(url, destination)
    except OSError as exc:
        raise WorkflowError("Kling download failed; resume queries the same task again: " + type(exc).__name__)
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise WorkflowError("Downloaded Kling file is missing or empty")
    size = destination.stat().st_size
    if declared is not None and size != declared:
        # The partial file is kept under its name; the next resume downloads to a new name.
        raise WorkflowError("Kling download truncated ({} of {} bytes); resume queries the same task again"
                            .format(size, declared))
    record.update(phase="downloaded", resource_id=record["generation_id"], file=str(destination.resolve()),
                  sha256=sha256(destination), size=size,
                  length_check="content_length" if declared is not None else "not_declared",
                  watermark="none" if works[0].get("urlWithoutWatermark") else "unknown",
                  playback_review="pending")
    write_state(directory, state)
    return record


JIMENG_COMMANDS = {"jimeng_frames": "frames2video", "jimeng_i2v": "image2video"}
JIMENG_SLOTS = {"jimeng_frames": ["first_frame", "last_frame"], "jimeng_i2v": ["first_frame"]}
JIMENG_FIELDS = {"mode", "prompt", "model_version", "duration", "video_resolution", "ratio"}
# Ranges from `dreamina frames2video -h` / `image2video -h`, dreamina 1.4.21 (2026-10-24).
JIMENG_MODELS = {"seedance2.5": ((4, 30), {"480p", "720p", "1080p"}),
                 "seedance2.0_vip": ((4, 15), {"720p", "1080p", "4k"}),
                 "seedance2.0": ((4, 15), {"720p"}), "seedance2.0fast": ((4, 15), {"720p"}),
                 "seedance2.0fast_vip": ((4, 15), {"720p"}), "seedance2.0mini": ((4, 15), {"720p"})}
JIMENG_RATIOS = {"1:1", "3:4", "16:9", "4:3", "9:16", "21:9"}


def jimeng_path():
    bundled = Path(__file__).resolve().parent / ".tools/dreamina/dreamina"
    found = str(bundled) if bundled.is_file() else shutil.which("dreamina")
    if not found:
        raise WorkflowError("dreamina CLI is not installed; see .tools/dreamina/README.md")
    return found


def call_jimeng(arguments, timeout=60):
    try:
        result = subprocess.run([jimeng_path()] + arguments, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise WorkflowError("dreamina CLI response unavailable: " + type(exc).__name__)
    # Failures (no login, no membership, validation) print plain text and exit nonzero.
    text = re.sub(r"https?://\S+", "<url>", ((result.stdout or "") + " " + (result.stderr or "")).strip())
    try:
        payload = json.loads(result.stdout.strip())
    except ValueError:
        payload = None
    if result.returncode != 0 or not isinstance(payload, (dict, list)):
        raise WorkflowError("dreamina CLI exit {}: {}".format(result.returncode, text[-400:] or "no output"))
    return payload


def jimeng_request(directory, shot):
    inputs = snapshot(directory, shot)
    request = inputs["video_request"]
    mode = request.get("mode")
    if mode not in JIMENG_COMMANDS:
        raise WorkflowError("Jimeng shots use video_request.mode jimeng_frames or jimeng_i2v")
    extra = set(request) - JIMENG_FIELDS
    if extra:
        raise WorkflowError("Unsupported Jimeng video_request fields: " + ", ".join(sorted(extra)))
    if not isinstance(request.get("prompt"), str) or not request["prompt"].strip():
        raise WorkflowError("Jimeng shots need video_request.prompt")
    if request["prompt"].startswith("--"):
        raise WorkflowError("Jimeng prompts cannot start with --; the CLI would read them as a flag")
    model = request.get("model_version")
    if model not in JIMENG_MODELS:
        raise WorkflowError("video_request.model_version must be one of: " + ", ".join(sorted(JIMENG_MODELS)))
    (low, high), resolutions = JIMENG_MODELS[model]
    duration = request.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, int) or not low <= duration <= high:
        raise WorkflowError("{} duration must be an integer from {} to {} seconds".format(model, low, high))
    if request.get("video_resolution") not in resolutions:
        raise WorkflowError("{} video_resolution must be one of: {}".format(model, ", ".join(sorted(resolutions))))
    if "ratio" in request and (model == "seedance2.5" or request["ratio"] not in JIMENG_RATIOS):
        # seedance2.5 follows the first frame and the CLI rejects an explicit ratio.
        raise WorkflowError("Omit video_request.ratio for seedance2.5; otherwise use one of " + ", ".join(sorted(JIMENG_RATIOS)))
    if any(ref.get("resource_id") or not ref.get("file") for ref in inputs["references"]):
        raise WorkflowError("Jimeng references must be local files")
    if sorted(ref.get("slot") for ref in inputs["references"]) != sorted(JIMENG_SLOTS[mode]):
        raise WorkflowError("{} needs exactly these reference slots: {}".format(mode, ", ".join(JIMENG_SLOTS[mode])))
    for ref in inputs["references"]:
        image_size(Path(ref["file"]))
    return inputs


def jimeng_estimate(project, request):
    rates = project["generation_authorization"].get("jimeng_credit_rate") or {}
    key = request["model_version"] + "@" + request["video_resolution"]
    if key not in rates:
        raise WorkflowError("Set generation_authorization.jimeng_credit_rate[{}] from the price shown on Jimeng".format(key))
    # The dreamina CLI has no quote command: estimate from the authorized per-second rate, rounded up.
    return (credits(rates[key]) * Decimal(request["duration"])).to_integral_value(rounding="ROUND_CEILING")


def jimeng_balance():
    """Account balance, or None when user_credit answers without an account (user_id 0 or missing)."""
    payload = call_jimeng(["user_credit"])
    if not isinstance(payload, dict):
        raise WorkflowError("user_credit did not return an object")
    if not payload.get("user_id"):
        return None
    return credits(payload.get("total_credit"))


def jimeng_checked_balance(project, state):
    """Live balance, or the user's web-checked balance minus what this project spent since that check."""
    live = jimeng_balance()
    if live is not None:
        return live, "user_credit"
    override = project["generation_authorization"].get("jimeng_balance_override")
    if not isinstance(override, dict) or not override.get("checked_at") or not override.get("source"):
        raise WorkflowError("user_credit returned no account; ask the user to check the balance on the Jimeng web "
                            "page and record generation_authorization.jimeng_balance_override")
    spent = Decimal("0")
    for record in state["operations"].values():
        if isinstance(record, dict) and (record.get("submitted_at") or "") > override["checked_at"]:
            spent += credits(record.get("actual_credits") or record.get("reserved_credits") or 0)
    return credits(override.get("credits")) - spent, "user_checked"


def jimeng_arguments(record):
    request = record["input"]["video_request"]
    files = {ref["slot"]: ref["file"] for ref in record["input"]["references"]}
    args = [JIMENG_COMMANDS[request["mode"]]]
    if request["mode"] == "jimeng_frames":
        args.extend(["--first", files["first_frame"], "--last", files["last_frame"]])
    else:
        args.extend(["--image", files["first_frame"]])
    args.extend(["--model_version", request["model_version"], "--duration", str(request["duration"]),
                 "--video_resolution", request["video_resolution"]])
    if "ratio" in request:
        args.extend(["--ratio", request["ratio"]])
    return args + ["--prompt", request["prompt"]]


def jimeng_task(payload):
    """The task object of a submit or query_result response."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload, dict):
        raise WorkflowError("dreamina CLI did not return a task object")
    return payload


def jimeng_save(directory, state, shot):
    inputs = jimeng_request(directory, shot)
    record = state["operations"].get(shot_key(shot))
    if record:
        return current_record(directory, state, shot)
    record = {"generator": "jimeng", "input": inputs, "phase": "saved",
              "quote_credits": None, "reserved_credits": None, "actual_credits": None}
    state["operations"][shot_key(shot)] = record
    write_state(directory, state)
    return record


def jimeng_quote(directory, project, state, shot):
    record = current_record(directory, state, shot)
    if record["phase"] not in ("saved", "quoted"):
        raise WorkflowError("Already submitted or uncertain; use resume, never quote-and-run again")
    amount = jimeng_estimate(project, record["input"]["video_request"])
    balance = jimeng_balance()
    record.update(phase="quoted", quote_credits=str(amount), account_credits=str(balance) if balance is not None else None,
                  quoted_at=timestamp())
    write_state(directory, state)
    return record


def jimeng_run(directory, project, state, shot):
    record = current_record(directory, state, shot)
    if record["phase"] not in ("saved", "quoted"):
        raise WorkflowError("This version was submitted or is uncertain; use resume or a new revision")
    amount = check_budget(project, state, shot, jimeng_estimate(project, record["input"]["video_request"]))
    before, balance_source = jimeng_checked_balance(project, state)
    if before < amount:
        raise WorkflowError("Jimeng account has {} credits; estimate needs {}".format(before, amount))
    record.update(phase="submitting", submit_id=str(uuid.uuid4()), reserved_credits=str(amount),
                  quote_credits=str(amount), authorization_ref=project["generation_authorization"]["approval_ref"],
                  balance_before=str(before), balance_source=balance_source, submitted_at=timestamp())
    write_state(directory, state)
    try:
        # Frames are uploaded before the submit; allow for upload time.
        task = jimeng_task(call_jimeng(jimeng_arguments(record), timeout=1800))
        generation_id = task.get("submit_id")
        if not isinstance(generation_id, str) or not generation_id:
            raise WorkflowError("Submission returned no submit_id")
    except WorkflowError as exc:
        record["phase"] = "needs_check"
        write_state(directory, state)
        raise WorkflowError("Jimeng submission is uncertain; check `dreamina list_task` and the Jimeng web history, "
                            "never resubmit this version: " + str(exc))
    # Persist the accepted task before interpreting any other field of the response.
    record.update(phase="submitted", generation_id=generation_id, task_status=str(task.get("gen_status", "")))
    write_state(directory, state)
    try:
        # The CLI reports no charge; record the balance change for reconciliation and keep the reservation.
        after = jimeng_balance()
        record["balance_after_submit"] = str(after) if after is not None else None
    except WorkflowError:
        record["balance_after_submit"] = None
    record["credit_status"] = "needs_check"
    write_state(directory, state)
    return record


def jimeng_resume(directory, state, shot):
    record = state["operations"].get(shot_key(shot))
    if not isinstance(record, dict) or not record.get("generation_id"):
        raise WorkflowError("No Jimeng submit_id; check `dreamina list_task` before taking further action")
    if record.get("phase") == "downloaded":
        path = Path(record.get("file") or "")
        if path.is_file() and sha256(path) == record.get("sha256"):
            return record
    output_dir = directory / "outputs" / shot_key(shot) / (record["generation_id"] + "-" + uuid.uuid4().hex[:8])
    output_dir.mkdir(parents=True)
    task = jimeng_task(call_jimeng(["query_result", "--submit_id", record["generation_id"],
                                    "--download_dir", str(output_dir)], timeout=600))
    status = str(task.get("gen_status", "")).lower()
    record["task_status"] = status
    files = sorted(path for path in output_dir.iterdir() if path.suffix.lower() == ".mp4" and path.stat().st_size > 0)
    if status == "querying":
        record["phase"] = "submitted"
    elif status == "fail":
        record.update(phase="failed", fail_reason=str(task.get("fail_reason", ""))[:300])
    elif status != "success" or len(files) != 1:
        record["phase"] = "needs_check"
    if status != "success" or len(files) != 1:
        if not any(output_dir.iterdir()):
            output_dir.rmdir()
        write_state(directory, state)
        return record
    record.update(phase="downloaded", resource_id=record["generation_id"], file=str(files[0].resolve()),
                  sha256=sha256(files[0]), size=files[0].stat().st_size, watermark="unknown",
                  playback_review="pending")
    write_state(directory, state)
    return record


def select_result(directory, state, shot, note):
    key = shot_key(shot)
    record = state["operations"].get(key)
    if not isinstance(record, dict) or record.get("phase") != "downloaded":
        raise WorkflowError("Only a downloaded result can be selected")
    path = Path(record.get("file") or "")
    if not path.is_file() or path.stat().st_size == 0:
        raise WorkflowError("The downloaded file is missing or empty")
    if not record.get("resource_id"):
        raise WorkflowError("The result has no confirmed resource_id")
    if sha256(path) != record.get("sha256"):
        raise WorkflowError("Selected file changed since download")
    state["selected"][shot["shot_id"]] = {"revision": shot["revision"],
        "resource_id": record["resource_id"], "file": str(path.resolve()),
        "sha256": record["sha256"],
        "note": note, "selected_at": timestamp()}
    write_state(directory, state)
    return state["selected"][shot["shot_id"]]


def batch_shots(episode):
    by_key = {shot_key(shot): shot for shot in episode["shots"]}
    order = episode.get("edit_order", list(by_key))
    if not isinstance(order, list) or not order or any(not isinstance(key, str) or key not in by_key for key in order):
        raise WorkflowError("edit_order must contain existing shot@revision keys")
    shots = [by_key[key] for key in order]
    if len({shot["shot_id"] for shot in shots}) != len(shots):
        raise WorkflowError("Choose exactly one revision per shot in edit_order before using --all")
    return shots


def execute_step(command, directory, project, state, shot):
    if project.get("generator", "dreamina") == "kling":
        if command == "save":
            return kling_save(directory, state, shot)
        if command == "quote":
            return kling_quote(directory, project, state, shot)
        if command == "run":
            return kling_run(directory, project, state, shot)
        return kling_resume(directory, state, shot)
    if project.get("generator") == "jimeng":
        if command == "save":
            return jimeng_save(directory, state, shot)
        if command == "quote":
            return jimeng_quote(directory, project, state, shot)
        if command == "run":
            return jimeng_run(directory, project, state, shot)
        return jimeng_resume(directory, state, shot)
    if project.get("generator", "dreamina") != "dreamina":
        raise WorkflowError("project.json generator must be dreamina, kling or jimeng")
    if command == "save":
        return save_shot(directory, project, state, shot)
    if command == "quote":
        return quote_shot(directory, state, shot)
    if command == "run":
        return run_shot(directory, project, state, shot)
    return resume_shot(directory, state, shot)


def execute_batch(command, directory, project, episode, state):
    results = []
    for shot in batch_shots(episode):
        key = shot_key(shot)
        record = state["operations"].get(key, {})
        phase = record.get("phase")
        if command in ("run", "quote") and phase not in (None, "saving", "saved", "quoted"):
            # A paid or uncertain version must never become a fresh submission.
            results.append({"shot": key, "status": "skipped", "phase": phase, "reason": "Use resume for the original task"})
            continue
        if command == "resume" and not record.get("submit_id"):
            results.append({"shot": key, "status": "skipped", "phase": phase, "reason": "No original submit_id"})
            continue
        if command == "resume" and record.get("generator") in ("kling", "jimeng") and not record.get("generation_id"):
            results.append({"shot": key, "status": "skipped", "phase": phase,
                            "reason": "No platform task id; check the platform's task history"})
            continue
        try:
            result = execute_step(command, directory, project, state, shot)
        except WorkflowError as exc:
            raise WorkflowError("Batch stopped at {} after {} processed item(s): {}".format(key, len(results), exc))
        results.append({"shot": key, "status": "processed", "phase": result.get("phase"),
                        "file": result.get("file"), "submit_id": result.get("submit_id")})
    return {"command": command, "items": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local single-shot workflow")
    parser.add_argument("--project-dir", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Read current inputs and state without contacting the platform")
    for command in ("save", "quote", "run", "resume", "select"):
        child = sub.add_parser(command)
        child.add_argument("--shot")
        child.add_argument("--revision")
        if command == "select":
            child.add_argument("--note", default="")
        else:
            child.add_argument("--all", action="store_true", help="Process edit_order serially; never resubmit existing tasks")
    child = sub.add_parser("reconcile", help="Kling/Jimeng: mark an uncertain submission as confirmed not submitted")
    child.add_argument("--shot", required=True)
    child.add_argument("--revision", required=True)
    child.add_argument("--not-submitted", action="store_true", required=True,
                       help="Confirm the platform's task history and credits show no task for this version")
    child.add_argument("--evidence", required=True, help="What was checked, e.g. app history, credits, CLI error")
    child = sub.add_parser("element", help="Kling only: create or look up a reusable subject from elements.json")
    choice = child.add_mutually_exclusive_group(required=True)
    choice.add_argument("--key")
    choice.add_argument("--all", action="store_true", help="Process every element in elements.json, stopping at a failure")
    args = parser.parse_args(argv)
    try:
        directory = args.project_dir.resolve()
        project, episode = load_inputs(directory)
        if args.command == "status":
            state = load_state(directory, project)
            print(json.dumps({"project_id": project["project_id"],
                              "shots": [shot_key(shot) for shot in episode["shots"]],
                              "state": state}, ensure_ascii=False, indent=2))
        else:
            with project_lock(directory):
                state = load_state(directory, project)
                if args.command == "reconcile":
                    if project.get("generator") not in ("kling", "jimeng"):
                        raise WorkflowError("reconcile requires project.json generator kling or jimeng")
                    result = kling_reconcile(directory, state, choose_shot(episode, args.shot, args.revision), args.evidence)
                elif args.command == "element":
                    if project.get("generator") != "kling":
                        raise WorkflowError("element requires project.json generator kling")
                    keys = list(load_elements(directory)) if args.all else [args.key]
                    result = [kling_element(directory, project, state, key) for key in keys]
                elif getattr(args, "all", False):
                    if args.shot or args.revision:
                        raise WorkflowError("--all cannot be combined with --shot/--revision")
                    result = execute_batch(args.command, directory, project, episode, state)
                else:
                    shot = choose_shot(episode, args.shot, args.revision)
                    if args.command == "select":
                        result = select_result(directory, state, shot, args.note)
                    else:
                        result = execute_step(args.command, directory, project, state, shot)
                print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except WorkflowError as exc:
        print("workflow: " + str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print("workflow: local file/process error: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
