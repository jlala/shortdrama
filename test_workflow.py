import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        root = Path(__file__).resolve().parent
        self.project = json.loads((root / "examples/project-input.json").read_text())
        self.shots = json.loads((root / "examples/episode-input.json").read_text())
        self.write_inputs()

    def tearDown(self):
        self.temporary.cleanup()

    def write_inputs(self):
        (self.directory / "project.json").write_text(json.dumps(self.project))
        (self.directory / "shots.json").write_text(json.dumps(self.shots))

    def test_missing_media_and_budget_allow_read_only_status(self):
        project, episode = workflow.load_inputs(self.directory)
        state = workflow.load_state(self.directory, project)
        self.assertFalse(project["generation_authorization"]["approved"])
        self.assertEqual(state["operations"], {})
        self.assertEqual(len(episode["shots"]), 1)
        self.assertFalse((self.directory / "state.json").exists())

    def test_mismatched_project_and_duplicate_revision_rejected(self):
        self.shots["project_id"] = "different"
        self.write_inputs()
        with self.assertRaises(workflow.WorkflowError):
            workflow.load_inputs(self.directory)
        self.shots["project_id"] = self.project["project_id"]
        self.shots["shots"].append(copy.deepcopy(self.shots["shots"][0]))
        self.write_inputs()
        with self.assertRaises(workflow.WorkflowError):
            workflow.load_inputs(self.directory)

    def test_unknown_and_nonfinite_credits_are_not_zero(self):
        for value in (None, True, "NaN", "Infinity", "-1", "bad"):
            with self.subTest(value=value), self.assertRaises(workflow.WorkflowError):
                workflow.credits(value)
        self.assertEqual(str(workflow.credits("0.25")), "0.25")

    def test_failed_replace_preserves_previous_state(self):
        state = workflow.load_state(self.directory, self.project)
        workflow.write_state(self.directory, state)
        original = (self.directory / "state.json").read_bytes()
        state["operations"]["shot@v01"] = {"phase": "submitting"}
        with patch("workflow.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                workflow.write_state(self.directory, state)
        self.assertEqual((self.directory / "state.json").read_bytes(), original)
        self.assertEqual(list(self.directory.glob(".state-*.json")), [])

    def test_second_executor_is_rejected(self):
        with workflow.project_lock(self.directory):
            with self.assertRaises(workflow.WorkflowError):
                with workflow.project_lock(self.directory):
                    pass
        with workflow.project_lock(self.directory):
            pass

    def test_authorization_and_pending_costs_block_generation(self):
        shot = self.shots["shots"][0]
        state = workflow.load_state(self.directory, self.project)
        with self.assertRaises(workflow.WorkflowError):
            workflow.check_budget(self.project, state, shot, 1)
        self.project["example_only"] = False
        self.project["generation_authorization"] = {"approved": True, "credit_cap": "10",
            "scope": [workflow.shot_key(shot)], "approval_ref": "test-user-approval"}
        state["operations"]["other@v01"] = {"phase": "submitted", "reserved_credits": "8"}
        with self.assertRaises(workflow.WorkflowError):
            workflow.check_budget(self.project, state, shot, 3)
        self.assertEqual(str(workflow.check_budget(self.project, state, shot, 2)), "2")
        state["operations"]["other@v01"]["reserved_credits"] = None
        with self.assertRaises(workflow.WorkflowError):
            workflow.check_budget(self.project, state, shot, 1)

    def saved_run(self):
        shot = copy.deepcopy(self.shots["shots"][0])
        shot["references"] = []
        shot["video_request"].update(mode="t2v", model="test-model", duration=4, ratio="9:16", resolution="720p")
        state = workflow.load_state(self.directory, self.project)
        record = {"input": workflow.snapshot(self.directory, shot), "phase": "quoted",
            "project_id": "11111111-1111-4111-8111-111111111111", "node_id": "node_0123456789",
            "generation": {}, "quote_credits": "3", "reserved_credits": None, "actual_credits": None}
        state["operations"][workflow.shot_key(shot)] = record
        self.project["example_only"] = False
        self.project["canvas_project_id"] = record["project_id"]
        self.project["generation_authorization"] = {"approved": True, "credit_cap": "10",
            "scope": [workflow.shot_key(shot)], "approval_ref": "test-user-approval"}
        return shot, state, record

    def test_uncertain_submission_saves_id_and_cannot_run_again(self):
        shot, state, record = self.saved_run()
        calls = []

        def response(directory, args):
            calls.append(args)
            if args[:2] == ["node", "confirm"]:
                return {"creditConfirmationToken": "ephemeral-test-token", "creditCeiling": 3}
            persisted = workflow.read_json(directory / "state.json")
            stored = persisted["operations"][workflow.shot_key(shot)]
            self.assertEqual(stored["phase"], "submitting")
            self.assertEqual(stored["submit_id"], args[args.index("--submit-id") + 1])
            self.assertEqual(stored["reserved_credits"], "3")
            raise workflow.WorkflowError("Lost submission response")

        with patch("workflow.quote_shot", return_value=record), patch("workflow.call_cli", side_effect=response):
            with self.assertRaises(workflow.WorkflowError):
                workflow.run_shot(self.directory, self.project, state, shot)
            original_id = record["submit_id"]
            self.assertEqual(record["phase"], "needs_check")
            with self.assertRaises(workflow.WorkflowError):
                workflow.run_shot(self.directory, self.project, state, shot)
        self.assertEqual(sum(args[:2] == ["node", "run"] for args in calls), 1)
        self.assertEqual(record["submit_id"], original_id)
        self.assertNotIn("ephemeral-test-token", (self.directory / "state.json").read_text())

    def test_missing_authorization_causes_no_cli_calls(self):
        shot, state, record = self.saved_run()
        self.project["generation_authorization"]["approved"] = False
        with patch("workflow.call_cli") as provider:
            with self.assertRaises(workflow.WorkflowError):
                workflow.run_shot(self.directory, self.project, state, shot)
        provider.assert_not_called()

    def test_latest_quote_over_cap_stops_before_confirmation(self):
        shot, state, record = self.saved_run()

        def updated_quote(*args):
            record["quote_credits"] = "11"
            return record

        with patch("workflow.quote_shot", side_effect=updated_quote), patch("workflow.call_cli") as provider:
            with self.assertRaises(workflow.WorkflowError):
                workflow.run_shot(self.directory, self.project, state, shot)
        provider.assert_not_called()
        self.assertNotIn("submit_id", record)

    def test_download_retry_queries_original_id_without_running(self):
        shot, state, record = self.saved_run()
        record.update(phase="submitted", submit_id="22222222-2222-4222-8222-222222222222", reserved_credits="3")
        resource_id = "33333333-3333-4333-8333-333333333333"
        calls = []
        downloads = 0

        def response(directory, args):
            nonlocal downloads
            calls.append(args)
            if args[:2] == ["operation", "status"]:
                self.assertEqual(args[2], record["submit_id"])
                return {"operationRef": record["submit_id"], "state": "succeeded",
                    "resources": [{"resourceId": resource_id, "state": "succeeded", "format": "mp4"}],
                    "submission": {"state": "completed"}}
            if args[:2] == ["resource", "get"]:
                return {"resourceId": resource_id, "status": "succeeded", "type": "video"}
            self.assertEqual(args[:2], ["resource", "download"])
            downloads += 1
            if downloads == 1:
                raise workflow.WorkflowError("Download interrupted")
            path = Path(args[args.index("--output") + 1])
            path.write_bytes(b"fixed-response-test-file")
            return {"resourceId": resource_id, "path": str(path), "size": path.stat().st_size,
                    "sha256": workflow.sha256(path)}

        with patch("workflow.call_cli", side_effect=response):
            with self.assertRaises(workflow.WorkflowError):
                workflow.resume_shot(self.directory, state, shot)
            workflow.resume_shot(self.directory, state, shot)
        self.assertEqual(record["phase"], "downloaded")
        self.assertEqual(downloads, 2)
        self.assertFalse(any(args[:2] in (["node", "run"], ["node", "confirm"]) for args in calls))
        self.assertIsNone(record["actual_credits"])
        workflow.select_result(self.directory, state, shot, "test choice")
        Path(record["file"]).write_bytes(b"changed")
        with self.assertRaises(workflow.WorkflowError):
            workflow.select_result(self.directory, state, shot, "cannot select changed file")

    def test_empty_successful_resources_are_not_completed(self):
        shot, state, record = self.saved_run()
        record.update(phase="submitted", submit_id="22222222-2222-4222-8222-222222222222")
        with patch("workflow.call_cli", return_value={"operationRef": record["submit_id"],
                "state": "succeeded", "resources": []}) as provider:
            workflow.resume_shot(self.directory, state, shot)
        self.assertEqual(record["phase"], "needs_check")
        self.assertEqual(provider.call_count, 1)

    def test_cli_json_failure_on_stderr_is_understood(self):
        payload = {"schemaVersion": "1", "ok": False, "error": {"code": "service.10000",
            "reason": "vip_membership_required", "requiredAction": "none", "requestId": "request-test"}}
        response = Mock(returncode=12, stdout="", stderr=json.dumps(payload))
        with patch("workflow.subprocess.run", return_value=response), patch("workflow.cli_path", return_value="dreamina-canvas"):
            with self.assertRaisesRegex(workflow.WorkflowError, "vip_membership_required"):
                workflow.call_cli(self.directory, ["canvas", "ls"])

    def test_batch_run_skips_submitted_and_uncertain_versions(self):
        shot, state, record = self.saved_run()
        second = copy.deepcopy(shot)
        second["shot_id"] = "E01-SC01-SH002"
        third = copy.deepcopy(shot)
        third["shot_id"] = "E01-SC01-SH003"
        episode = {"shots": [shot, second, third]}
        record.update(phase="needs_check", submit_id="original-id", reserved_credits="3")
        state["operations"][workflow.shot_key(second)] = {"phase": "downloaded", "reserved_credits": "3"}
        state["operations"][workflow.shot_key(third)] = {"phase": "quoted"}
        with patch("workflow.execute_step", return_value={"phase": "submitted"}) as step:
            result = workflow.execute_batch("run", self.directory, self.project, episode, state)
        self.assertEqual(step.call_count, 1)
        self.assertEqual(step.call_args.args[-1]["shot_id"], third["shot_id"])
        self.assertEqual([item["status"] for item in result["items"]], ["skipped", "skipped", "processed"])
        self.assertEqual(record["submit_id"], "original-id")

    def test_batch_stops_at_failure_without_touching_remaining_shots(self):
        shot = self.shots["shots"][0]
        second = copy.deepcopy(shot)
        second["shot_id"] = "E01-SC01-SH002"
        episode = {"shots": [shot, second]}
        state = workflow.load_state(self.directory, self.project)
        with patch("workflow.execute_step", side_effect=workflow.WorkflowError("stop")) as step:
            with self.assertRaisesRegex(workflow.WorkflowError, "Batch stopped"):
                workflow.execute_batch("save", self.directory, self.project, episode, state)
        self.assertEqual(step.call_count, 1)


def png(width, height):
    """Smallest header the size reader needs; enough for local validation."""
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"


def declaration(max_elements=7, resolutions=("720p",)):
    """A trimmed copy of the 2026-10-24 who_am_i declaration for Kling 4.0."""
    durations = [str(value) for value in range(3, 31)]
    return {"availableModels": {
        "image_to_video": {"models": [{"model": "kling-video-v4_0",
            "arguments": [{"name": "prompt"}, {"name": "duration", "allowedValues": durations},
                          {"name": "resolution", "allowedValues": list(resolutions)},
                          {"name": "aspect_ratio", "allowedValues": ["16:9", "9:16", "1:1", "21:9"]}],
            "inputs": [{"name": slot} for slot in workflow.KLING_SLOTS],
            "combinedItemCap": {"maxItems": 10, "arguments": [], "inputPrefixes": ["first_image", "tail_image", "keyframe_"]}}]},
        "omni_ref_video": {"models": [{"model": "kling-video-v4_0",
            "arguments": [{"name": "prompt"}, {"name": "elements"}, {"name": "duration", "allowedValues": durations},
                          {"name": "aspect_ratio", "allowedValues": ["auto", "21:9", "16:9", "9:16", "1:1"]},
                          {"name": "resolution", "allowedValues": list(resolutions)},
                          {"name": "enable_audio", "allowedValues": ["true", "false"]}],
            "inputs": [{"name": "image_{}".format(n)} for n in range(1, 11)] + [{"name": "video_{}".format(n)} for n in range(1, 6)],
            "combinedItemCap": {"maxItems": 15, "arguments": ["elements"], "inputPrefixes": ["image_", "video_", "audio_"]},
            "elementSupport": {"imageElements": True, "videoElements": True, "maxImageElements": max_elements,
                               "maxVideoElements": 3, "maxTotalElements": max_elements}}]}}}


class KlingWorkflowTests(unittest.TestCase):
    """Kling CLI integration, exercised with fixed responses only; nothing is submitted."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        for name in ("first.png", "tail.png", "front.png", "side.png"):
            (self.directory / name).write_bytes(png(941, 1672))
        self.project = {"schema_version": "0.2", "example_only": False, "project_id": "kling-test",
            "generator": "kling", "canvas_project_id": None,
            "generation_authorization": {"approved": True, "credit_cap": "100", "scope": ["*"],
                "approval_ref": "test-user-approval", "kling_credit_rate": {"image_to_video:kling-video-v4_0@720p": "5.5",
                                                            "omni_ref_video:kling-video-v4_0@720p": "6"}}}
        self.shot = {"shot_id": "E01-SC01-SH003", "revision": "v02", "planned_edit_seconds": 3,
            "references": [{"file": "first.png", "slot": "first_image"}, {"file": "tail.png", "slot": "tail_image"}],
            "video_request": {"mode": "kling_i2v", "model": "kling-video-v4_0", "prompt": "盒盖竖直提起后放到右侧。\n只有一个盒盖。",
                              "duration": 5, "resolution": "720p"}}
        (self.directory / "project.json").write_text(json.dumps(self.project))
        (self.directory / "shots.json").write_text(json.dumps(
            {"schema_version": "0.2", "project_id": "kling-test", "shots": [self.shot]}))
        self.state = workflow.load_state(self.directory, self.project)

    def tearDown(self):
        self.temporary.cleanup()

    def saved(self):
        return workflow.execute_step("save", self.directory, self.project, self.state, self.shot)

    def test_save_is_local_and_validates_slots(self):
        with patch("workflow.call_kling") as provider:
            record = self.saved()
        provider.assert_not_called()
        self.assertEqual(record["phase"], "saved")
        self.assertEqual(record["input"]["references"][0]["sha256"], workflow.sha256(self.directory / "first.png"))
        bad = copy.deepcopy(self.shot)
        bad["revision"] = "v03"
        for change in ({"duration": 31}, {"duration": 5.0}, {"ratio": "9:16"}, {"prompt": "--model other"}):
            with self.subTest(change=change), self.assertRaises(workflow.WorkflowError):
                bad["video_request"] = dict(self.shot["video_request"], **change)
                workflow.kling_save(self.directory, self.state, bad)
        bad["video_request"] = self.shot["video_request"]
        bad["references"] = [{"file": "tail.png", "slot": "tail_image"}]
        with self.assertRaises(workflow.WorkflowError):
            workflow.kling_save(self.directory, self.state, bad)

    def test_missing_authorization_or_rate_causes_no_cli_calls(self):
        self.saved()
        for change in ({"approved": False}, {"kling_credit_rate": {}}, {"credit_cap": "27"}):
            project = copy.deepcopy(self.project)
            project["generation_authorization"].update(change)
            with self.subTest(change=change), patch("workflow.call_kling") as provider:
                with self.assertRaises(workflow.WorkflowError):
                    workflow.kling_run(self.directory, project, self.state, self.shot)
                provider.assert_not_called()
        self.assertNotIn("submit_id", self.state["operations"]["E01-SC01-SH003@v02"])

    def test_example_project_never_submits(self):
        self.saved()
        self.project["example_only"] = True
        with patch("workflow.call_kling") as provider, self.assertRaises(workflow.WorkflowError):
            workflow.kling_run(self.directory, self.project, self.state, self.shot)
        provider.assert_not_called()

    def test_insufficient_account_credits_stops_before_submission(self):
        self.saved()
        calls = []

        def response(args, timeout=60):
            calls.append(args)
            return declaration() if args == ["who_am_i"] else {"availableRemainCredits": 0}

        with patch("workflow.call_kling", side_effect=response), self.assertRaisesRegex(workflow.WorkflowError, "paid credits"):
            workflow.kling_run(self.directory, self.project, self.state, self.shot)
        self.assertEqual(calls, [["who_am_i"], ["account"]])
        self.assertEqual(self.state["operations"]["E01-SC01-SH003@v02"]["phase"], "saved")

    def test_submission_is_journaled_first_and_never_repeated(self):
        self.saved()
        calls = []

        def response(args, timeout=60):
            calls.append(args)
            if args == ["who_am_i"]:
                return declaration()
            if args == ["account"]:
                return {"availableRemainCredits": 500}
            stored = workflow.read_json(self.directory / "state.json")["operations"]["E01-SC01-SH003@v02"]
            self.assertEqual(stored["phase"], "submitting")
            self.assertEqual(stored["reserved_credits"], "28")
            self.assertIn("--input", args)
            self.assertIn("first_image=" + str((self.directory / "first.png").resolve()), args)
            self.assertEqual(args[args.index("--prompt") + 1], "盒盖竖直提起后放到右侧。\n只有一个盒盖。")
            self.assertFalse(any(item.startswith("--prompt=") for item in args))
            return {"generationId": "gen-123", "creditsConsumed": 27}

        with patch("workflow.call_kling", side_effect=response):
            record = workflow.kling_run(self.directory, self.project, self.state, self.shot)
            with self.assertRaises(workflow.WorkflowError):
                workflow.kling_run(self.directory, self.project, self.state, self.shot)
            result = workflow.execute_batch("run", self.directory, self.project, {"shots": [self.shot]}, self.state)
        self.assertEqual(record["phase"], "submitted")
        self.assertEqual(record["generation_id"], "gen-123")
        self.assertEqual(record["actual_credits"], "27")
        self.assertEqual(sum(args[0] == "image_to_video" for args in calls), 1)
        self.assertEqual(result["items"][0]["status"], "skipped")

    def test_lost_submission_response_needs_check_and_blocks_retry(self):
        self.saved()

        def response(args, timeout=60):
            if args == ["who_am_i"]:
                return declaration()
            if args == ["account"]:
                return {"availableRemainCredits": 500}
            raise workflow.WorkflowError("Kling CLI response unavailable: TimeoutExpired")

        with patch("workflow.call_kling", side_effect=response) as provider:
            with self.assertRaisesRegex(workflow.WorkflowError, "never resubmit"):
                workflow.kling_run(self.directory, self.project, self.state, self.shot)
            with self.assertRaises(workflow.WorkflowError):
                workflow.kling_run(self.directory, self.project, self.state, self.shot)
            skipped = workflow.execute_batch("resume", self.directory, self.project, {"shots": [self.shot]}, self.state)
        record = self.state["operations"]["E01-SC01-SH003@v02"]
        self.assertEqual(record["phase"], "needs_check")
        self.assertEqual(provider.call_count, 3)
        self.assertEqual(skipped["items"][0]["status"], "skipped")
        # The reserved estimate keeps occupying the cap until reconciled.
        self.assertEqual(workflow.read_json(self.directory / "state.json")["operations"]["E01-SC01-SH003@v02"]["reserved_credits"], "28")

    def test_reconcile_only_clears_confirmed_unsent_submissions(self):
        record = self.saved()
        with self.assertRaises(workflow.WorkflowError):
            workflow.kling_reconcile(self.directory, self.state, self.shot, "nothing to reconcile")
        record.update(phase="needs_check", submit_id="local-id", reserved_credits="50")
        with self.assertRaises(workflow.WorkflowError):
            workflow.kling_reconcile(self.directory, self.state, self.shot, " ")
        workflow.kling_reconcile(self.directory, self.state, self.shot, "App history empty; credits unchanged")
        self.assertEqual(record["phase"], "saved")
        self.assertIsNone(record["reserved_credits"])
        self.assertNotIn("submit_id", record)
        self.assertEqual(record["reconciliations"][0]["submit_id"], "local-id")
        record.update(phase="needs_check", submit_id="other", generation_id="gen-1")
        with self.assertRaises(workflow.WorkflowError):
            workflow.kling_reconcile(self.directory, self.state, self.shot, "has a task id")

    def test_resume_polls_original_task_and_downloads_without_resubmitting(self):
        record = self.saved()
        record.update(phase="submitted", submit_id="local-id", generation_id="gen-123", reserved_credits="28")
        url = "https://example.invalid/signed-result.mp4?token=secret"
        answers = iter([{"status": "processing", "works": []},
                        {"status": "succeed", "works": [{"urlWithoutWatermark": url}]}])
        calls = []

        def response(args, timeout=60):
            calls.append(args)
            return next(answers)

        def fetch(source, destination):
            self.assertEqual(source, url)
            with destination.open("xb") as stream:
                stream.write(b"fixed-video")
            return len(b"fixed-video")

        with patch("workflow.call_kling", side_effect=response), patch("workflow.kling_fetch", side_effect=fetch):
            self.assertEqual(workflow.kling_resume(self.directory, self.state, self.shot)["phase"], "submitted")
            workflow.kling_resume(self.directory, self.state, self.shot)
        self.assertEqual(calls, [["query_tasks", "gen-123"]] * 2)
        self.assertEqual(record["phase"], "downloaded")
        self.assertEqual(record["watermark"], "none")
        self.assertNotIn("secret", (self.directory / "state.json").read_text())
        workflow.select_result(self.directory, self.state, self.shot, "test choice")
        self.assertEqual(self.state["selected"]["E01-SC01-SH003"]["resource_id"], "gen-123")

    def test_truncated_download_is_not_completed_and_can_resume(self):
        record = self.saved()
        record.update(phase="submitted", submit_id="local-id", generation_id="gen-123", reserved_credits="28")
        success = {"status": "succeed", "works": [{"urlWithoutWatermark": "https://example.invalid/v.mp4"}]}

        class Response(io.BytesIO):
            def __init__(self, data, declared):
                super().__init__(data)
                self.headers = {"Content-Length": declared}

        responses = iter([Response(b"truncated", "1000"), Response(b"x" * 1000, "1000")])
        with patch("workflow.call_kling", return_value=success), \
                patch("workflow.urllib.request.urlopen", side_effect=lambda url, timeout: next(responses)):
            with self.assertRaisesRegex(workflow.WorkflowError, "truncated"):
                workflow.kling_resume(self.directory, self.state, self.shot)
            self.assertEqual(record["phase"], "submitted")
            self.assertEqual(workflow.read_json(self.directory / "state.json")["operations"]["E01-SC01-SH003@v02"]["phase"], "saved")
            workflow.kling_resume(self.directory, self.state, self.shot)
        self.assertEqual(record["phase"], "downloaded")
        self.assertEqual(record["size"], 1000)
        self.assertEqual(record["length_check"], "content_length")
        self.assertNotEqual(Path(record["file"]).name, "gen-123.mp4")
        self.assertEqual((self.directory / "outputs/E01-SC01-SH003@v02/gen-123.mp4").read_bytes(), b"truncated")

    def test_unparseable_charge_keeps_task_id_and_reserved_estimate(self):
        self.saved()

        def response(args, timeout=60):
            if args == ["who_am_i"]:
                return declaration()
            if args == ["account"]:
                return {"availableRemainCredits": 500}
            return {"generationId": "gen-456", "creditsConsumed": ""}

        with patch("workflow.call_kling", side_effect=response):
            record = workflow.kling_run(self.directory, self.project, self.state, self.shot)
        stored = workflow.read_json(self.directory / "state.json")["operations"]["E01-SC01-SH003@v02"]
        for value in (record, stored):
            self.assertEqual(value["phase"], "submitted")
            self.assertEqual(value["generation_id"], "gen-456")
            self.assertEqual(value["credit_status"], "needs_check")
            self.assertIsNone(value["actual_credits"])
            self.assertEqual(value["reserved_credits"], "28")
        with patch("workflow.call_kling", return_value={"status": "processing"}) as provider:
            workflow.kling_resume(self.directory, self.state, self.shot)
        provider.assert_called_once_with(["query_tasks", "gen-456"])

    def omni_shot(self):
        (self.directory / "elements.json").write_text(json.dumps({"schema_version": "0.2", "elements": [
            {"key": "test_character", "name": "测试角色", "description": "通用测试角色，短发蓝色外套", "tags": ["角色"],
             "cover": "front.png", "secondary": ["side.png"]}]}))
        (self.directory / "motion.mp4").write_bytes(b"reference-video")
        shot = {"shot_id": "E01-SC01-SH009", "revision": "v01", "planned_edit_seconds": 9,
                "references": [{"file": "first.png", "slot": "image_1"}, {"file": "motion.mp4", "slot": "video_1"}],
                "video_request": {"mode": "kling_omni", "model": "kling-video-v4_0", "duration": 5, "resolution": "720p",
                                  "aspect_ratio": "9:16", "enable_audio": False, "elements": ["test_character"],
                                  "prompt": "单一连续镜头。<<<test_character>>>按图片1的构图，参考视频1的动作拿起杯子"}}
        return shot

    def create_element(self, responses):
        calls = []

        def response(args, timeout=60):
            calls.append(args)
            answer = responses[args[0]]
            if isinstance(answer, Exception):
                raise answer
            return answer(args) if callable(answer) else answer

        with patch("workflow.call_kling", side_effect=response):
            try:
                return workflow.kling_element(self.directory, self.project, self.state, "test_character"), calls
            except workflow.WorkflowError:
                return None, calls

    def test_live_declaration_blocks_unavailable_settings_before_spending(self):
        self.saved()
        self.shot["revision"] = "v03"
        self.shot["video_request"] = dict(self.shot["video_request"], resolution="1080p")
        self.project["generation_authorization"]["kling_credit_rate"]["image_to_video:kling-video-v4_0@1080p"] = "8"
        workflow.kling_save(self.directory, self.state, self.shot)
        calls = []

        def response(args, timeout=60):
            calls.append(args)
            return declaration()

        with patch("workflow.call_kling", side_effect=response), self.assertRaisesRegex(workflow.WorkflowError, "1080p"):
            workflow.kling_run(self.directory, self.project, self.state, self.shot)
        self.assertEqual(calls, [["who_am_i"]])
        self.assertEqual(self.state["operations"]["E01-SC01-SH003@v03"]["phase"], "saved")
        trimmed = declaration()
        trimmed["availableModels"]["image_to_video"]["models"][0]["inputs"] = [{"name": "first_image"}]
        saved = self.state["operations"]["E01-SC01-SH003@v02"]["input"]
        with self.assertRaisesRegex(workflow.WorkflowError, "tail_image"):
            workflow.kling_validate(trimmed, saved["video_request"], saved, [])

    def test_local_image_rules_follow_the_declaration(self):
        (self.directory / "first.png").write_bytes(b"not-an-image")
        with self.assertRaisesRegex(workflow.WorkflowError, "PNG or JPG"):
            workflow.kling_save(self.directory, self.state, self.shot)
        (self.directory / "first.png").write_bytes(png(3000, 1000))
        with self.assertRaisesRegex(workflow.WorkflowError, "aspect"):
            workflow.kling_save(self.directory, self.state, self.shot)
        self.assertNotIn("E01-SC01-SH003@v02", self.state["operations"])

    def test_element_creation_is_journaled_and_recovered_by_unique_name(self):
        self.omni_shot()
        record, calls = self.create_element({"element_list": {"elements": []},
                                              "element_create": workflow.WorkflowError("lost response")})
        self.assertIsNone(record)
        stored = workflow.read_json(self.directory / "state.json")["elements"]["test_character"]
        self.assertEqual(stored["phase"], "needs_check")
        remote_name = stored["input"]["remote_name"]
        self.assertRegex(remote_name, r"^测试角色 #[0-9a-f]{8}$")
        create = [args for args in calls if args[0] == "element_create"][0]
        self.assertEqual(create[create.index("--tag") + 1], "角色")
        self.assertIn(str((self.directory / "side.png").resolve()), create)
        # The earlier creation did succeed remotely: the rerun adopts it instead of creating again.
        record, calls = self.create_element({"element_list": {"elements": [{"id": 777, "name": remote_name}]},
                                              "element_get": {"id": 777, "resource": {"cover": "https://x.invalid/c"}}})
        self.assertEqual(record["phase"], "created")
        self.assertEqual((record["element_id"], record["type"]), ("777", "image"))
        self.assertFalse(any(args[0] == "element_create" for args in calls))
        with patch("workflow.call_kling") as provider:
            workflow.kling_element(self.directory, self.project, self.state, "test_character")
        provider.assert_not_called()

    def test_omni_submission_binds_elements_and_bills_input_video(self):
        shot = self.omni_shot()
        self.create_element({"element_list": {"elements": []}, "element_create": {"id": "123"},
                             "element_get": {"id": "123", "resource": {"cover": "https://x.invalid/c"}}})
        workflow.kling_save(self.directory, self.state, shot)
        calls = []

        def response(args, timeout=60):
            calls.append(args)
            if args == ["who_am_i"]:
                return declaration()
            if args == ["account"]:
                return {"availableRemainCredits": 500}
            return {"generationId": "gen-789", "creditsConsumed": 54}

        with patch("workflow.call_kling", side_effect=response), patch("workflow.media_seconds", return_value=4.0):
            record = workflow.kling_run(self.directory, self.project, self.state, shot)
        submit = calls[-1]
        self.assertEqual(submit[0], "omni_ref_video")
        self.assertEqual(json.loads(submit[submit.index("--elements") + 1]),
                         [{"id": "123", "bindName": "测试角色", "type": "image"}])
        self.assertEqual(submit[submit.index("--enable_audio") + 1], "false")
        self.assertEqual(submit[submit.index("--prompt") + 1], "单一连续镜头。<<<123>>>按图片1的构图，参考视频1的动作拿起杯子")
        self.assertIn("video_1=" + str((self.directory / "motion.mp4").resolve()), submit)
        # 6 credits/s for 5 output seconds plus 4 seconds of reference video.
        self.assertEqual(record["reserved_credits"], "54")
        self.assertEqual(record["input_seconds"], 4.0)

    def test_omni_rejects_unbound_changed_or_excess_elements(self):
        shot = self.omni_shot()
        workflow.kling_save(self.directory, self.state, shot)
        with patch("workflow.call_kling") as provider, patch("workflow.media_seconds", return_value=4.0):
            with self.assertRaisesRegex(workflow.WorkflowError, "Create element"):
                workflow.kling_run(self.directory, self.project, self.state, shot)
        provider.assert_not_called()
        self.create_element({"element_list": {"elements": []}, "element_create": {"id": "123"},
                             "element_get": {"id": "123", "resource": {}}})
        request, inputs = shot["video_request"], self.state["operations"]["E01-SC01-SH009@v01"]["input"]
        bound = workflow.kling_bound_elements(self.directory, self.state, request)
        with self.assertRaisesRegex(workflow.WorkflowError, "elementSupport"):
            workflow.kling_validate(declaration(max_elements=0), request, inputs, bound)
        (self.directory / "side.png").write_bytes(png(941, 1600))
        with self.assertRaisesRegex(workflow.WorkflowError, "changed after creation"):
            workflow.kling_bound_elements(self.directory, self.state, request)
        bad = copy.deepcopy(shot)
        bad["revision"] = "v02"
        bad["video_request"]["prompt"] = "图片1里的女孩抬起铁盒"
        with self.assertRaisesRegex(workflow.WorkflowError, "<<<key>>>"):
            workflow.kling_save(self.directory, self.state, bad)

    def test_local_cli_failure_reports_stderr(self):
        response = Mock(returncode=1, stdout="", stderr="ValidationError: no files were uploaded and no generation was submitted. https://x.invalid/s?sig=secret")
        with patch("workflow.subprocess.run", return_value=response), patch("workflow.kling_path", return_value="kling"):
            with self.assertRaisesRegex(workflow.WorkflowError, "no generation was submitted") as caught:
                workflow.call_kling(["image_to_video"])
        self.assertNotIn("secret", str(caught.exception))

    def test_cli_failure_hides_signed_urls(self):
        payload = {"ok": False, "status": 400, "body": {"error": "bad https://cdn.invalid/x?sig=secret"}}
        response = Mock(returncode=1, stdout=json.dumps(payload), stderr="")
        with patch("workflow.subprocess.run", return_value=response), patch("workflow.kling_path", return_value="kling"):
            with self.assertRaises(workflow.WorkflowError) as caught:
                workflow.call_kling(["account"])
        self.assertNotIn("secret", str(caught.exception))
        self.assertIn("<url>", str(caught.exception))


class JimengWorkflowTests(unittest.TestCase):
    """即梦 dreamina CLI integration, exercised with fixed responses only; nothing is submitted."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        for name in ("first.png", "last.png"):
            (self.directory / name).write_bytes(png(941, 1672))
        self.project = {"schema_version": "0.2", "example_only": False, "project_id": "jimeng-test",
            "generator": "jimeng", "canvas_project_id": None,
            "generation_authorization": {"approved": True, "credit_cap": "300", "scope": ["*"],
                "approval_ref": "test-user-approval", "jimeng_credit_rate": {"seedance2.5@720p": "26"}}}
        self.shot = {"shot_id": "E01-SC01-SH006", "revision": "v01", "planned_edit_seconds": 3,
            "references": [{"file": "first.png", "slot": "first_frame"}, {"file": "last.png", "slot": "last_frame"}],
            "video_request": {"mode": "jimeng_frames", "model_version": "seedance2.5", "duration": 4,
                              "video_resolution": "720p", "prompt": "外婆看着铁盒，身体不前倾。\n不说话。"}}
        (self.directory / "project.json").write_text(json.dumps(self.project))
        (self.directory / "shots.json").write_text(json.dumps(
            {"schema_version": "0.2", "project_id": "jimeng-test", "shots": [self.shot]}))
        self.state = workflow.load_state(self.directory, self.project)
        self.key = "E01-SC01-SH006@v01"

    def tearDown(self):
        self.temporary.cleanup()

    def saved(self):
        return workflow.execute_step("save", self.directory, self.project, self.state, self.shot)

    def test_save_is_local_and_validates_cli_rules(self):
        with patch("workflow.call_jimeng") as provider:
            record = self.saved()
        provider.assert_not_called()
        self.assertEqual(record["generator"], "jimeng")
        bad = copy.deepcopy(self.shot)
        bad["revision"] = "v02"
        for change in ({"ratio": "9:16"}, {"duration": 31}, {"duration": 3}, {"duration": 4.0},
                       {"video_resolution": "4k"}, {"model_version": "seedance3"}, {"prompt": "--ratio 16:9"},
                       {"model_version": "seedance2.0_vip", "duration": 16}, {"seed": 1}):
            with self.subTest(change=change), self.assertRaises(workflow.WorkflowError):
                bad["video_request"] = dict(self.shot["video_request"], **change)
                workflow.jimeng_save(self.directory, self.state, bad)
        bad["video_request"] = dict(self.shot["video_request"], model_version="seedance2.0_vip", ratio="9:16")
        workflow.jimeng_save(self.directory, self.state, bad)
        bad["revision"] = "v03"
        bad["video_request"] = self.shot["video_request"]
        bad["references"] = [{"file": "first.png", "slot": "first_frame"}]
        with self.assertRaises(workflow.WorkflowError):
            workflow.jimeng_save(self.directory, self.state, bad)

    def test_missing_authorization_or_rate_causes_no_cli_calls(self):
        self.saved()
        for change in ({"approved": False}, {"jimeng_credit_rate": {}}, {"credit_cap": "103"}):
            project = copy.deepcopy(self.project)
            project["generation_authorization"].update(change)
            with self.subTest(change=change), patch("workflow.call_jimeng") as provider:
                with self.assertRaises(workflow.WorkflowError):
                    workflow.jimeng_run(self.directory, project, self.state, self.shot)
                provider.assert_not_called()
        self.project["example_only"] = True
        with patch("workflow.call_jimeng") as provider, self.assertRaises(workflow.WorkflowError):
            workflow.jimeng_run(self.directory, self.project, self.state, self.shot)
        provider.assert_not_called()

    def test_insufficient_balance_stops_before_submission(self):
        self.saved()
        with patch("workflow.call_jimeng", return_value={"total_credit": 80, "user_id": 1}) as provider, \
                self.assertRaisesRegex(workflow.WorkflowError, "80 credits"):
            workflow.jimeng_run(self.directory, self.project, self.state, self.shot)
        provider.assert_called_once_with(["user_credit"])
        self.assertEqual(self.state["operations"][self.key]["phase"], "saved")

    def test_submission_is_journaled_first_and_never_repeated(self):
        self.saved()
        calls = []
        balances = iter([725, 621])

        def response(args, timeout=60):
            calls.append(args)
            if args == ["user_credit"]:
                return {"total_credit": next(balances), "user_id": 1, "vip_level": "basic"}
            stored = workflow.read_json(self.directory / "state.json")["operations"][self.key]
            self.assertEqual(stored["phase"], "submitting")
            self.assertEqual(stored["reserved_credits"], "104")
            self.assertEqual(args[:5], ["frames2video", "--first", str((self.directory / "first.png").resolve()),
                                        "--last", str((self.directory / "last.png").resolve())])
            self.assertNotIn("--ratio", args)
            self.assertEqual(args[args.index("--prompt") + 1], "外婆看着铁盒，身体不前倾。\n不说话。")
            return {"submit_id": "sub-123", "gen_status": "querying"}

        with patch("workflow.call_jimeng", side_effect=response):
            record = workflow.jimeng_run(self.directory, self.project, self.state, self.shot)
            with self.assertRaises(workflow.WorkflowError):
                workflow.jimeng_run(self.directory, self.project, self.state, self.shot)
            result = workflow.execute_batch("run", self.directory, self.project, {"shots": [self.shot]}, self.state)
        self.assertEqual(record["phase"], "submitted")
        self.assertEqual(record["generation_id"], "sub-123")
        self.assertEqual((record["balance_before"], record["balance_after_submit"]), ("725", "621"))
        # The CLI reports no charge, so the reservation keeps occupying the cap.
        self.assertIsNone(record["actual_credits"])
        self.assertEqual(sum(args[0] == "frames2video" for args in calls), 1)
        self.assertEqual(result["items"][0]["status"], "skipped")

    def test_lost_submission_needs_check_until_reconciled(self):
        self.saved()

        def response(args, timeout=60):
            if args == ["user_credit"]:
                return {"total_credit": 725, "user_id": 1}
            raise workflow.WorkflowError("dreamina CLI response unavailable: TimeoutExpired")

        with patch("workflow.call_jimeng", side_effect=response) as provider:
            with self.assertRaisesRegex(workflow.WorkflowError, "never resubmit"):
                workflow.jimeng_run(self.directory, self.project, self.state, self.shot)
            with self.assertRaises(workflow.WorkflowError):
                workflow.jimeng_run(self.directory, self.project, self.state, self.shot)
            skipped = workflow.execute_batch("resume", self.directory, self.project, {"shots": [self.shot]}, self.state)
        record = self.state["operations"][self.key]
        self.assertEqual(record["phase"], "needs_check")
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(skipped["items"][0]["status"], "skipped")
        workflow.kling_reconcile(self.directory, self.state, self.shot, "list_task empty; balance unchanged at 725")
        self.assertEqual(record["phase"], "saved")
        self.assertNotIn("balance_before", record)

    def test_resume_queries_original_task_and_keeps_every_download(self):
        record = self.saved()
        record.update(phase="submitted", submit_id="local-id", generation_id="sub-123", reserved_credits="104")
        calls = []

        def response(args, timeout=60):
            calls.append(args)
            target = Path(args[args.index("--download_dir") + 1])
            self.assertEqual(list(target.iterdir()), [])
            if len(calls) == 1:
                return {"submit_id": "sub-123", "gen_status": "querying"}
            (target / "result.mp4").write_bytes(b"fixed-video")
            return {"submit_id": "sub-123", "gen_status": "success"}

        with patch("workflow.call_jimeng", side_effect=response):
            self.assertEqual(workflow.jimeng_resume(self.directory, self.state, self.shot)["phase"], "submitted")
            workflow.jimeng_resume(self.directory, self.state, self.shot)
            workflow.jimeng_resume(self.directory, self.state, self.shot)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(args[:3] == ["query_result", "--submit_id", "sub-123"] for args in calls))
        self.assertEqual(record["phase"], "downloaded")
        self.assertEqual(Path(record["file"]).read_bytes(), b"fixed-video")
        workflow.select_result(self.directory, self.state, self.shot, "test choice")
        self.assertEqual(self.state["selected"]["E01-SC01-SH006"]["resource_id"], "sub-123")

    def test_failed_task_records_reason_without_resubmitting(self):
        record = self.saved()
        record.update(phase="submitted", submit_id="local-id", generation_id="sub-9", reserved_credits="104")
        with patch("workflow.call_jimeng", return_value={"gen_status": "fail", "fail_reason": "content check"}):
            workflow.jimeng_resume(self.directory, self.state, self.shot)
        self.assertEqual((record["phase"], record["fail_reason"]), ("failed", "content check"))
        with self.assertRaises(workflow.WorkflowError):
            workflow.jimeng_run(self.directory, self.project, self.state, self.shot)

    def test_empty_user_credit_needs_a_user_checked_balance(self):
        self.saved()
        empty = {"total_credit": 0, "user_id": 0, "user_name": "", "vip_level": ""}
        with patch("workflow.call_jimeng", return_value=empty) as provider, \
                self.assertRaisesRegex(workflow.WorkflowError, "jimeng_balance_override"):
            workflow.jimeng_run(self.directory, self.project, self.state, self.shot)
        provider.assert_called_once_with(["user_credit"])
        self.assertEqual(self.state["operations"][self.key]["phase"], "saved")
        self.project["generation_authorization"]["jimeng_balance_override"] = {
            "credits": "200", "checked_at": "2026-10-26T00:00:00+00:00", "source": "user checked web page"}
        self.state["operations"]["E01-SC01-SH001@v01"] = {"phase": "submitted", "reserved_credits": "120",
                                                         "submitted_at": "2026-10-26T01:00:00+00:00"}

        def response(args, timeout=60):
            if args == ["user_credit"]:
                return empty
            return {"submit_id": "sub-9", "gen_status": "querying"}

        # 200 checked minus 120 spent since the check leaves 80, below the 104 estimate.
        with patch("workflow.call_jimeng", side_effect=response), self.assertRaisesRegex(workflow.WorkflowError, "80"):
            workflow.jimeng_run(self.directory, self.project, self.state, self.shot)
        del self.state["operations"]["E01-SC01-SH001@v01"]
        with patch("workflow.call_jimeng", side_effect=response):
            record = workflow.jimeng_run(self.directory, self.project, self.state, self.shot)
        self.assertEqual((record["balance_before"], record["balance_source"]), ("200", "user_checked"))
        self.assertIsNone(record["balance_after_submit"])

    def test_plain_text_cli_failure_is_reported_without_links(self):
        response = Mock(returncode=1, stdout="登录成功，但当前账号没有 dreamina_cli 使用权限: 仅限会员用户使用 https://x.invalid/a?t=1\n",
                        stderr="")
        with patch("workflow.subprocess.run", return_value=response), \
                patch("workflow.jimeng_path", return_value="dreamina"), \
                self.assertRaisesRegex(workflow.WorkflowError, "仅限会员") as caught:
            workflow.call_jimeng(["user_credit"])
        self.assertNotIn("x.invalid", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
