"""Owner-only phone entry point, using the existing bot process and model controls."""
from __future__ import annotations
import fcntl
import json
import threading
from pathlib import Path
import answer_workflow as aw
from conversation_memory import Memory, identity

_THREAD = None
_THREAD_LOCK = threading.Lock()
COMMANDS = ("/work", "/workpublic", "/reply", "/workstatus")


def enabled(root):
    return (Path(root) / "config/answer-workflow-phone.enabled").exists()


def sources(root, question, public=False):
    result = json.loads((Path(root) / "config/workflow-sources.json").read_text())
    if public:
        return result  # A public question does not authorize exporting private RAG.
    # Optional existing research memory. Failure leaves the named static sources,
    # never turns a missing retrieval into a claim that research was performed.
    try:
        from cirrus_rag import retrieve
        for i, row in enumerate(retrieve(question, top_k=2)):
            result["digest-" + str(i)] = str(row["text"])[:1600]
    except Exception:
        pass
    return result


def status(row):
    labels = {"queued": "Queued", "running": "Working", "ready": "Answer ready",
              "delivered": "Answer ready", "needs_review": "Needs review",
              "delivery_unknown": "Answer saved; notification unconfirmed"}
    delivery = ("\nThe earlier notification was not confirmed; this is the saved result."
                if row["meta"].get("notice_attempted") and not row["meta"].get("notice_confirmed") else "")
    return ("Request " + row["id"] + "\n" + labels.get(row["state"], row["state"]) + delivery +
            "\n" + row["meta"].get("reason", "") +
            ("\n\n" + row["answer"] if row["answer"] else "") +
            "\n\nFollow up: /reply " + row["id"] + " your correction or next question")


def handle(message, chat_id, *, root, allowed_id):
    if not enabled(root):
        return "The answer pilot is not enabled on this server."
    if (message.get("from", {}).get("id") != allowed_id or chat_id != allowed_id
            or message.get("chat", {}).get("type", "private") != "private"):
        return "This workflow is available only in Buddy's private bot chat."
    command, _, arg = message.get("text", "").strip().partition(" ")
    memory = Memory(root)
    client = "telegram:" + str(allowed_id)
    if command == "/workstatus":
        if not arg:
            rows = memory.recent("phone", client)
            return "\n\n".join(status(row)[:550] for row in rows) or "No workflow requests yet."
        row = memory.get(arg.strip())
        if not row or row["owner"] != "phone" or row["client"] != client:
            return "No matching request in this chat."
        return status(row)
    parent = None
    if command == "/reply":
        key, _, arg = arg.partition(" ")
        parent = memory.get(key)
        if not parent or parent["owner"] != "phone" or parent["client"] != client:
            return "No matching request in this chat."
        if parent["state"] in ("queued", "running"):
            return "That request is still working. Check /workstatus " + key
    question = arg.strip()
    if not question or len(question) > 6000:
        return "Use /work followed by a question (up to 6000 characters), or /reply ID your correction."
    if not message.get("message_id"):
        return "Missing message identity; request was not queued."
    import dev_loop
    tier, reason = dev_loop.classify_risk("CIRRUS_NOTE", question)
    if tier == dev_loop.TIER_NEVER:
        return "This answer-only workflow cannot take that action. Use the existing reviewed development queue."
    row = memory.add(owner="phone", client=client, project="buddy",
        thread=parent["thread"] if parent else identity(client, str(message["message_id"])),
        message_id=str(message["message_id"]), question=question,
        privacy=parent["privacy"] if parent else ("CLOUD_ALLOWED" if command == "/workpublic" else "LOCAL_ONLY"),
        meta={"chat_id": chat_id, "source": "telegram", "parent": parent["id"] if parent else ""})
    return ("Request " + row["id"] + " " + ("queued." if row["state"] == "queued" else "already recorded.") +
            "\nI'll prepare a checked answer. No shell commands or outside messages are part of this workflow."
            "\nCheck /workstatus " + row["id"])


def drain(root, creds, notify, *, models=None, get_sources=None):
    memory = Memory(root)
    with (memory.directory / "phone-worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        memory.recover("phone")
        while True:
            row = memory.claim("phone")
            if row is None:
                break
            try:
                row = aw.compose(memory, row, models or aw.Models(root, creds),
                                 (get_sources or sources)(root, row["question"], row["privacy"] == "CLOUD_ALLOWED"))
            except Exception as exc:
                row = aw.hold(memory, row, "Workflow failed: " + type(exc).__name__)
        for row in memory.pending_notices("phone"):
            # Checkpoint BEFORE sending. Unknown Telegram delivery is never retried
            # automatically; the durable result remains available via /workstatus.
            if not row["meta"].get("notice_attempted"):
                memory.update(row["id"], meta={"notice_attempted": True})
                try:
                    confirmed = bool(notify(row["meta"]["chat_id"], status(row)))
                except Exception:
                    confirmed = False
                memory.update(row["id"], meta={"notice_confirmed": confirmed})


def kick(root, creds, notify):
    global _THREAD
    if not enabled(root):
        return
    with _THREAD_LOCK:
        if _THREAD is None or not _THREAD.is_alive():
            _THREAD = threading.Thread(target=drain, args=(root, creds, notify), daemon=True)
            _THREAD.start()


def selftest():
    import unittest
    from test_answer_workflow import BotTests
    return unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(BotTests)).wasSuccessful()


if __name__ == "__main__":
    import sys
    sys.exit(0 if len(sys.argv) == 2 and sys.argv[1] == "--selftest" and selftest() else 1)
