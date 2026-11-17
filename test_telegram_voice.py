#!/usr/bin/env python3
"""S653 owner-only Telegram voice: refusals, local-only flow, no words or token in logs.

Runs no model, speech engine, network or bot: every command, download and
upload is a fake, and all files live in temporary directories.
Usage: python3 test_telegram_voice.py
"""
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import telegram_voice as tv

OWNER = 4242
TOKEN = "123456:SECRET-test-token"
QUESTION = "what is the status of the morning digest"


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Post:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def __call__(self, url, data=None, files=None, timeout=None):
        self.calls.append((url, data, files["voice"][1].read()))
        return mock.Mock(json=lambda: {"ok": self.ok})


class Harness:
    def __init__(self, transcript=" what is the  status of\nthe morning digest ", post_ok=True):
        self.sent, self.logs, self.asked, self.runs, self.api = [], [], [], [], []
        self.transcript, self.post = transcript, Post(post_ok)

    def api_call(self, method, params):
        self.api.append((method, params))
        return {"ok": True, "result": {"file_path": "voice/file_7.oga"}}

    def opener(self, url, timeout=None):
        return Resp(b"OggS-fake-recording")

    def run(self, cmd, **kw):
        self.runs.append(cmd)
        if cmd[0] == "STT":
            Path(cmd[2], Path(cmd[1]).stem + ".txt").write_text(self.transcript)
        elif cmd[0] == "TTS":
            Path(cmd[1]).write_bytes(b"raw-audio")
        elif cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"OggS-answer")

    def ask(self, q):
        self.asked.append(q)
        return "🧠 *CIRRUS Memory Answer*\n\nThe digest ran at 07:00.\n\n_Sources: 2026-11-16_"

    def handle(self, message, chat_id=OWNER):
        tv.handle(message, chat_id, allowed_id=OWNER, token=TOKEN, api_call=self.api_call,
                  send_text=self.sent.append, ask=self.ask, log=self.logs.append, host="CIRRUS",
                  run=self.run, opener=self.opener, post=self.post)


def voice_message(duration=6, sender=OWNER, size=9000):
    return {"message_id": 9, "from": {"id": sender}, "chat": {"id": sender, "type": "private"},
            "voice": {"file_id": "abc", "duration": duration, "file_size": size}}


FAKE_STT = lambda audio, out: ["STT", str(audio), str(out)]
FAKE_TTS = lambda text_file, raw: (["TTS", str(raw) + ".aiff"], Path(str(raw) + ".aiff"))


@mock.patch.object(tv, "stt_command", FAKE_STT)
@mock.patch.object(tv, "tts_command", FAKE_TTS)
class HandleTests(unittest.TestCase):
    def assert_private_logs(self, h):
        joined = "\n".join(h.logs)
        self.assertNotIn(TOKEN, joined)
        self.assertNotIn("SECRET", joined)
        self.assertNotIn("morning digest", joined)

    def test_owner_voice_is_transcribed_answered_and_spoken(self):
        h = Harness()
        h.handle(voice_message())
        self.assertEqual(h.asked, [QUESTION])
        self.assertEqual(h.sent[0], "🎙️ Heard on CIRRUS:\n" + QUESTION)
        self.assertIn("The digest ran at 07:00.", h.sent[1])
        self.assertEqual(len(h.sent), 2)
        url, data, body = h.post.calls[0]
        self.assertTrue(url.endswith("/sendVoice"))
        self.assertEqual((data, body), ({"chat_id": OWNER}, b"OggS-answer"))
        self.assertEqual([c[0] for c in h.runs], ["STT", "TTS", "ffmpeg"])
        self.assertIn("libopus", h.runs[2])
        self.assertIn("Voice reply delivered", h.logs)
        self.assert_private_logs(h)

    def test_temporary_audio_is_removed(self):
        h = Harness()
        h.handle(voice_message())
        work = Path(h.runs[0][2])
        self.assertFalse(work.exists())

    def test_other_chat_is_refused_before_download(self):
        h = Harness()
        h.handle(voice_message(), chat_id=-1001)
        self.assertEqual((h.api, h.asked, h.runs), ([], [], []))
        self.assertIn("only in Buddy's private chat", h.sent[0])

    def test_other_sender_is_refused_before_download(self):
        h = Harness()
        msg = voice_message(sender=99)
        h.handle(msg, chat_id=OWNER)
        self.assertEqual((h.api, h.asked, h.runs), ([], [], []))

    def test_long_recording_is_refused(self):
        h = Harness()
        h.handle(voice_message(duration=tv.MAX_SECONDS + 1))
        self.assertEqual((h.api, h.asked), ([], []))
        self.assertIn("nothing was asked", h.sent[0])

    def test_download_error_never_logs_the_token_url(self):
        h = Harness()

        def leaky(url, timeout=None):
            raise OSError("failed fetching " + url)
        h.opener = leaky
        h.handle(voice_message())
        self.assertEqual(h.asked, [])
        self.assertIn("Voice transcription failed: OSError", h.logs)
        self.assertIn("Nothing was asked", h.sent[0])
        self.assert_private_logs(h)

    def test_silence_asks_nothing(self):
        h = Harness(transcript="  \n")
        h.handle(voice_message())
        self.assertEqual(h.asked, [])
        self.assertIn("could not hear any words", h.sent[0])

    def test_no_local_transcriber_never_falls_back(self):
        h = Harness()
        with mock.patch.object(tv, "stt_command", side_effect=tv.Unavailable("no local whisper")):
            h.handle(voice_message())
        self.assertEqual((h.asked, h.post.calls), ([], []))
        self.assertIn("no local speech-to-text", h.sent[0])

    def test_no_local_voice_sends_text_only_and_says_so(self):
        h = Harness()
        with mock.patch.object(tv, "tts_command", side_effect=tv.Unavailable("no local voice")):
            h.handle(voice_message())
        self.assertEqual(h.asked, [QUESTION])
        self.assertEqual(h.post.calls, [])
        self.assertIn("no local voice installed", h.sent[-1])

    def test_rejected_voice_upload_is_reported(self):
        h = Harness(post_ok=False)
        h.handle(voice_message())
        self.assertIn("spoken reply failed", h.sent[-1])


class SpeechTextTests(unittest.TestCase):
    def test_markup_emoji_links_and_sources_are_not_spoken(self):
        said = tv.speech_text("🧠 *CIRRUS Answer*\n\nSee `cirrus_bot` at https://x.example/a?b=1 now."
                              "\n\n_Sources: 2026-11-01, 2026-11-02_")
        self.assertEqual(said, "CIRRUS Answer\nSee cirrus bot at link now.")

    def test_long_answers_are_cut_with_a_pointer_to_the_text(self):
        said = tv.speech_text("word " * 2000)
        self.assertLessEqual(len(said), tv.SPOKEN_LIMIT + 60)
        self.assertTrue(said.endswith("The rest of the answer is in the text message."))


class EngineTests(unittest.TestCase):
    def test_missing_cached_model_is_refused_rather_than_downloaded(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(Path, "home", return_value=Path(home)):
            with self.assertRaises(tv.Unavailable):
                tv.stt_command("a.oga", home)

    def test_cached_model_uses_local_cli_in_english(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(Path, "home", return_value=Path(home)):
            Path(home, ".cache/whisper").mkdir(parents=True)
            Path(home, ".cache/whisper/small.pt").write_bytes(b"")
            with mock.patch.object(tv.shutil, "which", return_value="/usr/local/bin/whisper"):
                cmd = tv.stt_command("a.oga", "/w")
        self.assertEqual(cmd[:2], ["/usr/local/bin/whisper", "a.oga"])
        self.assertEqual(cmd[cmd.index("--model") + 1], "small")
        self.assertEqual(cmd[cmd.index("--language") + 1], "en")


class WiringTests(unittest.TestCase):
    def test_bot_checks_the_owner_before_handling_voice(self):
        src = (Path(__file__).resolve().parent / "cirrus_bot.py").read_text()
        loop = src[src.index("def run_bot"):]
        self.assertLess(loop.index("if user_id != ALLOWED_ID"), loop.index('message.get("voice")'))
        self.assertIn('"text": "/ask " + q', loop)


class VideoMessageTests(unittest.TestCase):
    """The real run_bot loop, compiled alone with fakes (no Telegram, no config)."""

    def run_loop(self, message):
        import ast
        tree = ast.parse((Path(__file__).resolve().parent / "cirrus_bot.py").read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_bot")
        sent, handled, logs = [], [], []
        with tempfile.TemporaryDirectory() as root:
            scope = {"PROJECT_DIR": Path(root), "ALLOWED_ID": OWNER, "CREDS": {}, "log": logs.append,
                     "api_call": mock.Mock(side_effect=[{"result": [{"update_id": 1, "message": message}]},
                                                        KeyboardInterrupt()]),
                     "handle_message": lambda *a: handled.append(a) or "unknown",
                     "send_message": lambda chat, text: sent.append((chat, text)),
                     "check_heartbeats": lambda: None}
            exec(compile(ast.Module(body=[fn], type_ignores=[]), "cirrus_bot.py", "exec"), scope)
            with mock.patch("bot_workflow.kick"), mock.patch.dict("os.environ", {"SKYWARDEN_TELEGRAM_INBOX": ""}):
                scope["run_bot"]()
        return sent, handled

    def test_owner_video_message_gets_the_microphone_hint(self):
        sent, handled = self.run_loop({"message_id": 3, "from": {"id": OWNER},
                                       "chat": {"id": OWNER, "type": "private"},
                                       "video_note": {"file_id": "v", "duration": 4}})
        self.assertEqual(sent, [(OWNER, tv.VIDEO_HINT)])
        self.assertEqual(handled, [])
        self.assertIn("switch back to the microphone", tv.VIDEO_HINT)

    def test_other_senders_video_is_ignored(self):
        sent, handled = self.run_loop({"message_id": 3, "from": {"id": 99}, "chat": {"id": 99},
                                       "video_note": {"file_id": "v", "duration": 4}})
        self.assertEqual((sent, handled), ([], []))


if __name__ == "__main__":
    unittest.main()
