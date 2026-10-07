"""Exercise persisted recovery, tenant boundaries, actual routing and send gates."""
import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import answer_workflow as aw
import bot_workflow as bw
from conversation_memory import Memory

SOURCE = {"note": "The planning meeting is Thursday at 10 AM."}


def output(ok=True, answer="The planning meeting is Thursday at 10 AM.", quote=None):
    return json.dumps({"answer": answer, "confidence": .95 if ok else .4,
        "verdict": "pass" if ok else "needs_review", "kind": "grounded",
        "evidence": [{"source": "note", "quote": quote or SOURCE["note"]}],
        "reason": "The supplied note states the time."})


class FakeModels:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def call(self, payload, **kwargs):
        self.calls.append((json.loads(json.dumps(payload)), kwargs))
        provider, raw = next(self.replies)
        if isinstance(raw, BaseException):
            raise raw
        return {"provider": provider, "model": "fixture", "raw": raw}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.m = Memory(self.root)

    def add(self, n="1", **kwargs):
        args = dict(owner="phone", client="buddy", project="test", thread="topic",
                    message_id=n, question="When is the planning meeting?", privacy="CLOUD_ALLOWED")
        args.update(kwargs)
        return self.m.add(**args)

    def test_tenant_and_host_boundaries(self):
        self.add("a", client="bill")
        self.add("b", project="other")
        self.add("c", owner="intake")
        self.add("d", thread="other")
        row = self.add("e")
        self.assertEqual(self.m.history(row), [])
        self.assertEqual(len(self.m.history(row, same_thread=False)), 1)

    def test_duplicate_and_atomic_claim(self):
        row = self.add()
        repeated = self.add(question="Different payload with the same message ID")
        self.assertEqual(row["question"], repeated["question"])
        other = Memory(self.root)
        self.assertEqual(other.claim("phone")["id"], row["id"])
        self.assertIsNone(self.m.claim("phone"))
        self.assertTrue(other.transition(row["id"], "running", "ready"))
        self.assertFalse(self.m.transition(row["id"], "running", "ready"))

    def test_restart_states_and_private_permissions(self):
        safe = self.add("safe", state="running", meta={"stages": {"local": {"parsed": None}}})
        unknown = self.add("unknown", state="running", meta={"active_stage": "foundation"})
        delivery = self.add("delivery", state="sending")
        self.m.recover("phone")
        self.assertEqual(self.m.get(safe["id"])["state"], "queued")
        self.assertEqual(self.m.get(unknown["id"])["state"], "needs_review")
        self.assertEqual(self.m.get(delivery["id"])["state"], "delivery_unknown")
        self.assertEqual(self.m.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.m.directory.stat().st_mode & 0o777, 0o700)


class WorkflowTests(MemoryTests):
    def test_operator_review_releases_future_client_followups_only(self):
        row = self.add(owner="intake", state="needs_review", meta={"requires_buddy": True})
        later = self.add("later", owner="intake")
        held = aw.compose(self.m, later, FakeModels([]), SOURCE)
        self.assertEqual(held["state"], "needs_review")
        self.m.thread_review(later, "Reviewed the original question and corrected the source.")
        model = FakeModels([("ollama", output()), ("ollama", output())])
        next_row = self.add("after", owner="intake")
        self.assertEqual(aw.compose(self.m, next_row, model, SOURCE)["state"], "ready")
        self.assertEqual(self.m.get(row["id"])["state"], "needs_review")

    def test_cold_qualified_model_is_warmed_without_question_data(self):
        import capability_health as health
        import capability_registry as registry
        import capability_admission as admission
        import capability_dispatch as dispatch
        import time
        config = self.root / "config"
        config.mkdir()
        (config / "answer-workflow-capabilities.json").write_text(json.dumps({
            "enabled": True, "max_user_bytes": 12000, "evaluations": [{
                "id": "ollama", "model": "fixture", "location": "local", "approved": True,
                "evaluated_at": time.time()-1, "expires_at": time.time()+100}]}))
        cold = {"id": "ollama", "status": "not_resident"}
        warm = {"id": "ollama", "status": "ready_metadata", "healthy": True}
        with patch.object(registry, "contract_digest", return_value="a"*64), \
             patch.object(health, "observe", side_effect=[cold, warm]) as observe, \
             patch.object(admission, "candidates", return_value=[warm]) as candidates, \
             patch.object(dispatch, "dispatch", return_value=("ollama", output())), \
             patch.object(aw.urllib.request, "urlopen") as urlopen:
            model = aw.Models(self.root, {"ollama_model": "fixture", "ollama_url": "http://localhost:11434"})
            result = model.call({"question": "PRIVATE FIXTURE"}, pool="local", exclude=(),
                                privacy="LOCAL_ONLY", session_id="test")
        self.assertEqual(result["provider"], "ollama")
        self.assertEqual(observe.call_count, 2)
        self.assertNotIn("PRIVATE FIXTURE", urlopen.call_args.args[0].data.decode())
        self.assertTrue(candidates.call_args.args[1][0]["healthy"])

    def test_renewal_is_quiet_until_due_and_never_auto_installs(self):
        import answer_workflow_eval as evaluation
        import time
        config = self.root / "config"
        config.mkdir()
        (config / "answer-workflow-phone.enabled").touch()
        path = config / "answer-workflow-capabilities.json"
        record = {"id": "ollama", "contract_sha256": "current", "expires_at": time.time()+3*86400}
        path.write_text(json.dumps({"evaluations": [record]}))
        with patch.object(evaluation, "ROOT", self.root), \
             patch.object(evaluation, "contract", return_value=({}, "current")), \
             patch.object(evaluation, "run", return_value=True) as run, \
             patch.object(evaluation, "install") as install:
            self.assertTrue(evaluation.renewal_check()[0])
            run.assert_not_called()
            record["expires_at"] = time.time()+86400
            path.write_text(json.dumps({"evaluations": [record]}))
            self.assertFalse(evaluation.renewal_check()[0])
            run.assert_called_once_with("ollama")
            install.assert_not_called()
        self.assertEqual(json.loads(path.read_text())["evaluations"][0], record)

    def test_local_first_and_independent_review(self):
        model = FakeModels([("ollama", output()), ("ollama", output())])
        result = aw.compose(self.m, self.add(), model, SOURCE)
        self.assertEqual(result["state"], "ready")
        self.assertEqual([x[1]["pool"] for x in model.calls], ["local", "local"])
        self.assertEqual(model.calls[-1][0]["mode"], "review")
        self.assertTrue(result["answer"].endswith("Is this what you were looking for?"))

    def test_foundation_reconciles_local_uncertainty(self):
        model = FakeModels([("ollama", output(False)), ("kimi", output())])
        result = aw.compose(self.m, self.add(), model, SOURCE)
        self.assertEqual(result["state"], "ready")
        self.assertEqual([x[1]["pool"] for x in model.calls], ["local", "cloud"])
        self.assertEqual(model.calls[-1][0]["drafts"][0]["provider"], "ollama")

    def test_panel_has_two_distinct_providers_and_local_reconciliation(self):
        model = FakeModels([("ollama", output(False)), ("kimi", output(False)),
                            ("anthropic", output()), ("ollama", output())])
        result = aw.compose(self.m, self.add(), model, SOURCE)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(model.calls[2][1]["exclude"], ("kimi",))
        self.assertEqual(len(model.calls[-1][0]["drafts"]), 3)
        self.assertEqual(model.calls[-1][0]["mode"], "review")

    def test_private_history_cannot_leak_to_cloud(self):
        self.add("old", privacy="LOCAL_ONLY")
        model = FakeModels([("ollama", output(False))])
        result = aw.compose(self.m, self.add("new"), model, SOURCE)
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0][1]["privacy"], "LOCAL_ONLY")
        self.assertTrue((self.m.directory / (result["id"] + "-review.json")).exists())

    def test_bad_evidence_and_malformed_json_fail(self):
        self.assertIsNone(aw.parse(output(quote="Friday at noon"), SOURCE))
        self.assertIsNone(aw.parse("not JSON", SOURCE))
        data = json.loads(output())
        data["evidence"] = []
        self.assertIsNone(aw.parse(json.dumps(data), SOURCE))

    def test_qualification_accepts_corrected_history_and_honest_abstention(self):
        import answer_workflow_eval as evaluation
        cases = dict(evaluation.fixtures())
        data = json.loads(output())
        data["answer"] = "Friday at 2 PM is the new time. Thursday at 10 AM was superseded."
        data["evidence"] = [{"source": "correction", "quote": cases["correction"]["sources"]["correction"]}]
        self.assertTrue(evaluation.score("correction", json.dumps(data), cases["correction"]))
        data["answer"] = "The planning meeting has been moved to Friday at 2 PM (updated from the earlier Thursday 10 AM time)."
        self.assertTrue(evaluation.score("correction", json.dumps(data), cases["correction"]))
        data["answer"] = "Thursday at 10 AM is the time, not Friday at 2 PM."
        self.assertFalse(evaluation.score("correction", json.dumps(data), cases["correction"]))
        data = json.loads(output())
        data["answer"] += " The Saturday claim is unsupported and rejected."
        self.assertTrue(evaluation.score("reconcile", json.dumps(data), cases["reconcile"]))
        data = json.loads(output(False))
        data["answer"] = "The supplied material does not establish an address."
        data["evidence"] = []
        self.assertTrue(evaluation.score("missing", json.dumps(data), cases["missing"]))
        data["answer"] = ""
        self.assertTrue(evaluation.score("missing", json.dumps(data), cases["missing"]))
        data["confidence"] = True
        self.assertIsNone(aw.parse(json.dumps(data), SOURCE))

    def test_same_provider_does_not_count_twice(self):
        model = FakeModels([("ollama", output(False)), ("kimi", output(False)), ("kimi", output())])
        result = aw.compose(self.m, self.add(), model, SOURCE)
        self.assertEqual(result["state"], "needs_review")

    def test_checkpoint_replay_does_not_repeat_completed_inference(self):
        parsed = aw.parse(output(), SOURCE)
        row = self.add(meta={"stages": {"local": {"provider": "ollama", "parsed": parsed}}})
        model = FakeModels([("ollama", output())])
        result = aw.compose(self.m, row, model, SOURCE)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0][0]["mode"], "review")

    def test_interruption_holds_unconfirmed_inference(self):
        row = self.add(state="running")
        with self.assertRaises(KeyboardInterrupt):
            aw.compose(self.m, row, FakeModels([("ollama", KeyboardInterrupt())]), SOURCE)
        self.m.recover("phone")
        self.assertEqual(self.m.get(row["id"])["state"], "needs_review")

    def test_no_headway_hold_and_one_progress_check(self):
        for n in ("a", "b"):
            row = self.add(n)
            self.m.update(row["id"], state="delivered", answer="Earlier answer " + n)
        result = aw.compose(self.m, self.add("c", question="Still wrong, the same answer again."),
                            FakeModels([]), SOURCE)
        self.assertEqual(result["state"], "needs_review")
        other = self.add("d", question="What about this instead?")
        result = aw.compose(self.m, other, FakeModels([]), SOURCE)
        self.assertTrue(result["meta"]["progress_check"])
        result = aw.compose(self.m, self.add("e", question="Please try again"), FakeModels([]), SOURCE)
        self.assertEqual(result["state"], "needs_review")

    def test_genuine_progress_is_not_stopped_by_turn_count(self):
        for n in range(4):
            row = self.add(str(n))
            self.m.update(row["id"], state="delivered", answer="Earlier answer")
        model = FakeModels([("ollama", output()), ("ollama", output())])
        result = aw.compose(self.m, self.add("next", question="Closer, when is the meeting?"), model, SOURCE)
        self.assertEqual(result["state"], "ready")

    def test_correction_survives_new_instance_and_new_topic(self):
        self.add("old", question="Correction: use plain paragraphs, not bullet lists.")
        row = self.add("new", thread="another", question="Suggest a meeting summary.")
        self.m = Memory(self.root)
        model = FakeModels([("ollama", output()), ("ollama", output())])
        aw.compose(self.m, row, model, SOURCE)
        values = list(model.calls[0][0]["sources"].values())
        self.assertIn("Correction: use plain paragraphs, not bullet lists.", values)

    def test_provider_exception_is_sanitized_and_no_cloud_for_private(self):
        model = FakeModels([("ollama", RuntimeError("private transport details"))])
        result = aw.compose(self.m, self.add(privacy="LOCAL_ONLY"), model, SOURCE)
        self.assertEqual(result["state"], "needs_review")
        self.assertNotIn("private transport details", json.dumps(result))


class BotTests(MemoryTests):
    def test_padded_private_command_is_not_written_to_bot_log(self):
        from unittest.mock import Mock
        tree = ast.parse((Path(aw.__file__).parent / "cirrus_bot.py").read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_bot")
        logs = []
        update = {"update_id": 1, "message": {"text": "  /work private fixture note", "message_id": 1,
                  "from": {"id": 7}, "chat": {"id": 7, "type": "private"}}}
        scope = {"PROJECT_DIR": self.root, "ALLOWED_ID": 7, "CREDS": {}, "log": logs.append,
                 "api_call": Mock(side_effect=[{"result": [update]}, KeyboardInterrupt()]),
                 "handle_message": lambda *_: "saved", "send_message": lambda *_: None,
                 "check_heartbeats": lambda: None}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "cirrus_bot.py", "exec"), scope)
        with patch.object(bw, "kick"):
            scope["run_bot"]()
        self.assertTrue(any("Workflow command received" in line for line in logs))
        self.assertFalse(any("private fixture" in line for line in logs))

    def setUp(self):
        super().setUp()
        (self.root / "config").mkdir()
        (self.root / "config/answer-workflow-phone.enabled").touch()

    def message(self, text="/work When is the planning meeting?", mid=100, user=7):
        return {"text": text, "message_id": mid, "from": {"id": user},
                "chat": {"id": user, "type": "private"}}

    def test_real_handler_queues_once_and_drains_to_saved_result(self):
        # Execute the real bot's entry-point function without importing live config.
        tree = ast.parse((Path(__file__).parent / "cirrus_bot.py").read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "handle_message")
        scope = {"PROJECT_DIR": self.root, "ALLOWED_ID": 7}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "cirrus_bot.py", "exec"), scope)
        text = scope["handle_message"](self.message(), 7)
        self.assertIn("queued", text)
        row = self.m.recent("phone", "telegram:7")[0]
        model = FakeModels([("ollama", output()), ("ollama", output())])
        notices = []
        bw.drain(self.root, {}, lambda chat, msg: notices.append((chat, msg)) or True,
                 models=model, get_sources=lambda *_: SOURCE)
        self.assertEqual(self.m.get(row["id"])["state"], "ready")
        self.assertEqual(len(notices), 1)
        scope["handle_message"](self.message(), 7)
        bw.drain(self.root, {}, lambda *_: self.fail("duplicate notification"), models=FakeModels([]))
        self.assertIn("Thursday", bw.handle(self.message("/workstatus " + row["id"], 101), 7,
                      root=self.root, allowed_id=7))
        self.assertEqual(len(model.calls), 2)

    def test_unauthorized_or_group_requests_never_queue(self):
        bw.handle(self.message(user=9), 9, root=self.root, allowed_id=7)
        msg = self.message()
        msg["chat"]["type"] = "group"
        bw.handle(msg, 7, root=self.root, allowed_id=7)
        self.assertEqual(self.m.recent("phone", "telegram:7"), [])

    def test_notification_failure_preserves_answer_without_resending(self):
        bw.handle(self.message(), 7, root=self.root, allowed_id=7)
        bw.drain(self.root, {}, lambda *_: False,
            models=FakeModels([("ollama", output()), ("ollama", output())]), get_sources=lambda *_: SOURCE)
        row = self.m.recent("phone", "telegram:7")[0]
        self.assertFalse(row["meta"]["notice_confirmed"])
        self.assertTrue(row["answer"])
        bw.drain(self.root, {}, lambda *_: self.fail("retried uncertain notification"), models=FakeModels([]))

    def test_public_mode_is_explicit_and_skips_private_retrieval(self):
        bw.handle(self.message("/workpublic Which server owns live client work?"), 7,
                  root=self.root, allowed_id=7)
        row = self.m.recent("phone", "telegram:7")[0]
        self.assertEqual(row["privacy"], "CLOUD_ALLOWED")
        (self.root / "config/workflow-sources.json").write_text(json.dumps(SOURCE))
        self.assertEqual(bw.sources(self.root, "meeting", public=True), SOURCE)


class IntakeTests(MemoryTests):
    def test_unknown_intake_option_cannot_start_live_processing(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, str(Path(aw.__file__).parent / "intake.py"),
                                 "--not-a-command"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown option", result.stderr)
        self.assertNotIn("no configured senders", result.stdout)

    def test_email_exception_is_sanitized_and_uncertain_send_is_held(self):
        import task_solver as ts
        rec = {"requester": "fixture-client", "projects": ["synthetic"], "message_id": "send-error",
               "body_head": "When is the planning meeting?", "kind": "answer", "title": "Meeting"}
        row = aw.intake_record(self.root, rec, "Meeting", "LOCAL_ONLY")
        self.m.update(row["id"], state="ready", answer=SOURCE["note"])
        with patch.object(ts, "PROJECT_DIR", self.root), \
             patch.object(ts, "_send_mail", side_effect=RuntimeError("sensitive fixture details")) as send, \
             patch.object(ts, "_fallback_to_ticket"):
            result = aw.solve_and_answer(rec, {}, "fixture@example.invalid", "Meeting")
            self.assertEqual(self.m.get(row["id"])["state"], "delivery_unknown")
            self.assertNotIn("sensitive fixture", result["reason"])
            aw.solve_and_answer(rec, {}, "fixture@example.invalid", "Meeting")
            self.assertEqual(send.call_count, 1)

    def test_full_original_question_retained_for_local_review(self):
        question = "Explain this supplied material: " + "detail " * 2000
        rec = {"requester": "fixture-client", "projects": ["synthetic"], "message_id": "long",
               "body_head": question[:2000], "conversation_body": question}
        row = aw.intake_record(self.root, rec, "Original question", "LOCAL_ONLY")
        packet = json.loads(self.m.review_packet(row).read_text())
        self.assertEqual(packet["original_question"], question.strip())

    def test_real_email_adapter_records_answer_and_suppresses_replay(self):
        import task_solver as ts
        rec = {"requester": "fixture-client", "projects": ["synthetic"], "message_id": "mail-1",
               "from_email": "fixture@example.invalid", "body_head": "When is the planning meeting?",
               "kind": "answer", "title": "Meeting"}
        good = json.loads(output())
        good["evidence"][0]["source"] = "project-record"
        model = FakeModels([("ollama", json.dumps(good)), ("ollama", json.dumps(good))])
        with patch.object(ts, "PROJECT_DIR", self.root), \
             patch.object(ts, "try_entity_kb_answer", return_value=SOURCE["note"]), \
             patch.object(ts, "_send_mail", return_value=True) as send, \
             patch.object(ts, "_record_promise"), patch.object(ts.dev_loop, "ledger_append"), \
             patch.object(aw, "Models", return_value=model):
            first = aw.solve_and_answer(rec, {}, "fixture@example.invalid", "Meeting")
            second = aw.solve_and_answer(rec, {}, "fixture@example.invalid", "Meeting")
        self.assertTrue(first["answered"])
        self.assertTrue(second["answered"])
        self.assertEqual(send.call_count, 1)
        row = self.m.get(first["request_id"])
        self.assertEqual(row["state"], "delivered")
        self.assertIn("Thursday", row["answer"])

    def test_unconfirmed_email_delivery_is_not_retried(self):
        import task_solver as ts
        rec = {"requester": "fixture-client", "projects": ["synthetic"], "message_id": "mail-2",
               "body_head": "When is the planning meeting?", "kind": "answer", "title": "Meeting"}
        row = aw.intake_record(self.root, rec, "Meeting", "LOCAL_ONLY")
        self.m.update(row["id"], state="ready", answer=SOURCE["note"])
        with patch.object(ts, "PROJECT_DIR", self.root), \
             patch.object(ts, "_send_mail", return_value=False) as send, \
             patch.object(ts, "_fallback_to_ticket"):
            first = aw.solve_and_answer(rec, {}, "fixture@example.invalid", "Meeting")
            second = aw.solve_and_answer(rec, {}, "fixture@example.invalid", "Meeting")
        self.assertFalse(first["answered"])
        self.assertFalse(second["answered"])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.m.get(row["id"])["state"], "delivery_unknown")


if __name__ == "__main__":
    unittest.main()
