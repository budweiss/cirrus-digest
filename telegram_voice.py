"""Owner-only Telegram voice messages, handled entirely on this host (S653).

A voice message from Buddy's private chat is downloaded to a private temporary
directory, transcribed HERE with the openai-whisper CLI and model already
cached on this host, echoed back, and answered by the same handler a typed
/ask uses. The answer is sent as text and, when this host has a local voice
(macOS `say` on CIRRUS, Piper on CUMULUS1), as a Telegram voice message in
Ogg/Opus. No cloud speech service is used, nothing is downloaded at request
time, and the audio, transcript and speech files are deleted when the request
finishes. Logs carry sizes and durations only, never the words or a URL.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

MAX_SECONDS = 300                 # a spoken question, not a dictated memo
MAX_BYTES = 20 * 1024 * 1024      # Telegram's getFile ceiling for bots
WHISPER_MODEL = "small"
SPOKEN_LIMIT = 3000               # characters; the full answer is always sent as text
PIPER_DIR = Path.home() / ".local/share/cowork-piper"
PIPER_VOICE = "en_US-lessac-medium.onnx"
TELEGRAM = "https://api.telegram.org"
# A quick tap flips Telegram's microphone button to its round-video camera (S654).
VIDEO_HINT = ("That was a video message, so nothing was asked. Telegram's button had "
              "switched to the camera. Tap it once to switch back to the microphone, "
              "then hold it and speak your question.")


class Unavailable(Exception):
    """This host has no local engine for the step; never a cloud fallback."""


def stt_command(audio, out_dir):
    model = Path.home() / ".cache/whisper" / (WHISPER_MODEL + ".pt")
    if not model.exists():
        raise Unavailable("no cached whisper model")  # whisper would download it
    exe = shutil.which("whisper")
    if exe:
        base = [exe]
    elif importlib.util.find_spec("whisper"):
        base = [sys.executable, "-m", "whisper"]
    else:
        raise Unavailable("no local whisper")
    return base + [str(audio), "--model", WHISPER_MODEL, "--language", "en",
                   "--task", "transcribe", "--output_format", "txt",
                   "--output_dir", str(out_dir), "--verbose", "False"]


def tts_command(text_file, raw):
    if sys.platform == "darwin" and shutil.which("say"):
        return ["say", "-o", str(raw) + ".aiff", "-f", str(text_file)], Path(str(raw) + ".aiff")
    piper, voice = PIPER_DIR / "venv/bin/piper", PIPER_DIR / PIPER_VOICE
    if piper.exists() and voice.exists():
        return ([str(piper), "-m", str(voice), "-i", str(text_file), "-f", str(raw) + ".wav"],
                Path(str(raw) + ".wav"))
    raise Unavailable("no local voice")


def engines():
    """One log line saying what this host would use; inspects, does not run."""
    try:
        stt = "whisper-" + WHISPER_MODEL if stt_command("x", "x") else ""
    except Unavailable as exc:
        stt = "none (" + str(exc) + ")"
    try:
        tts = Path(tts_command("x", "x")[0][0]).name
    except Unavailable as exc:
        tts = "none (" + str(exc) + ")"
    return "transcription=" + stt + ", speech=" + tts


def probe(run=subprocess.run):
    """Speak one word with this host's voice, in the bot's own service context."""
    try:
        with tempfile.TemporaryDirectory(prefix="cowork-voice-") as work:
            ogg = speak("Ready.", work, run)
            return "speech probe ok" if ogg.stat().st_size else "speech probe produced no audio"
    except Unavailable as exc:
        return "speech probe skipped: " + str(exc)
    except Exception as exc:
        return "speech probe FAILED: " + type(exc).__name__


def transcribe(audio, work, run=subprocess.run):
    run(stt_command(audio, work), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=180, check=True)
    return " ".join((Path(work) / (Path(audio).stem + ".txt")).read_text().split())


def speech_text(answer):
    """The answer as words to be spoken: no markup, emoji, links or source dates."""
    text = "\n".join(line for line in answer.splitlines()
                     if not line.strip().startswith("_Sources:"))
    text = re.sub(r"https?://\S+", "link", text)
    text = re.sub(r"[*_`#>\[\]|~]", " ", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("So", "Sk", "Cs", "Co"))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    if len(text) > SPOKEN_LIMIT:
        cut = text.rfind(" ", 0, SPOKEN_LIMIT)
        text = text[:cut if cut > 0 else SPOKEN_LIMIT] + ". The rest of the answer is in the text message."
    return text


def speak(text, work, run=subprocess.run):
    """Return the path of an Ogg/Opus file speaking `text`."""
    text_file = Path(work) / "answer.txt"
    text_file.write_text(text)
    cmd, raw = tts_command(text_file, Path(work) / "answer")
    run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=True)
    ogg = Path(work) / "answer.ogg"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
         "-c:a", "libopus", "-b:a", "32k", "-ac", "1", str(ogg)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=True)
    return ogg


def fetch(token, api_call, file_id, dest, opener=urllib.request.urlopen):
    info = api_call("getFile", {"file_id": file_id})
    path = (info.get("result") or {}).get("file_path") if info.get("ok") else None
    if not path:
        raise RuntimeError("Telegram returned no file path")
    # The URL holds the bot token: it must never reach a log or an exception text.
    with opener(TELEGRAM + "/file/bot" + token + "/" + urllib.parse.quote(path), timeout=60) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise RuntimeError("recording too large")
    Path(dest).write_bytes(data)
    return Path(dest)


def send_voice(token, chat_id, ogg, post=None):
    if post is None:
        import requests
        post = requests.post
    with open(ogg, "rb") as f:
        resp = post(TELEGRAM + "/bot" + token + "/sendVoice", data={"chat_id": chat_id},
                    files={"voice": ("answer.ogg", f, "audio/ogg")}, timeout=120)
    return bool(resp.json().get("ok"))


def handle(message, chat_id, *, allowed_id, token, api_call, send_text, ask, log, host,
           run=subprocess.run, opener=urllib.request.urlopen, post=None):
    """Answer one voice message. Never raises; every outcome is told to Buddy."""
    voice = message.get("voice") or {}
    if message.get("from", {}).get("id") != allowed_id or chat_id != allowed_id:
        log("Voice message refused outside Buddy's private chat")
        return send_text("Voice questions work only in Buddy's private chat with this bot.")
    seconds = int(voice.get("duration") or 0)
    if seconds > MAX_SECONDS or int(voice.get("file_size") or 0) > MAX_BYTES:
        return send_text(f"That recording is {seconds} seconds long. Voice questions are limited "
                         f"to {MAX_SECONDS // 60} minutes; nothing was asked.")
    with tempfile.TemporaryDirectory(prefix="cowork-voice-") as work:
        try:
            audio = fetch(token, api_call, voice.get("file_id", ""), Path(work) / "question.oga", opener)
            question = transcribe(audio, work, run)
        except Unavailable as exc:
            log(f"Voice message not transcribed: {exc}")
            return send_text(f"{host} has no local speech-to-text installed, so the voice message "
                             "was not transcribed. Nothing was asked.")
        except Exception as exc:
            log(f"Voice transcription failed: {type(exc).__name__}")
            return send_text(f"I could not transcribe that voice message on {host}. Nothing was "
                             "asked; please try again or type /ask.")
        if not question:
            log(f"Voice message had no words ({seconds}s audio)")
            return send_text("I could not hear any words in that voice message. Nothing was asked.")
        log(f"Voice question transcribed on this host ({seconds}s audio, {len(question)} characters)")
        send_text(f"🎙️ Heard on {host}:\n{question}")
        answer = ask(question)
        send_text(answer)
        try:
            delivered = send_voice(token, chat_id, speak(speech_text(answer), work, run), post)
        except Unavailable:
            log("Voice reply skipped: no local voice on this host")
            return send_text(f"(No spoken reply: {host} has no local voice installed. The text "
                             "answer above is complete.)")
        except Exception as exc:
            log(f"Voice reply failed: {type(exc).__name__}")
            delivered = False
        if delivered:
            log("Voice reply delivered")
        else:
            send_text(f"(The spoken reply failed on {host}. The text answer above is complete.)")
