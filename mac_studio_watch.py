#!/usr/bin/env python3
"""S406: official Apple Mac Studio watch, daily on CIRRUS; no LLM or laptop.

Default is a read-only preview. --live sends through the existing CIRRUS bot
and saves only successfully delivered snapshots. --status reads local state.
The store's memory selector is distinct from its storage selector. A listed
option is reported as listed, never promoted to a verified checkout or quote.
"""
import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

PROJECT_DIR = Path.home() / "projects/cirrus-digest"
STATE_PATH = PROJECT_DIR / "logs/mac-studio-watch.json"
URLS = {
    "store": "https://www.apple.com/shop/buy-mac/mac-studio",
    "specs": "https://www.apple.com/mac-studio/specs/",
}
MEMORY = "memory-dimensionMemory"
SECTIONS = ("section-chip", "section-memory", "section-storage")


def normalize(text):
    return " ".join(text.replace("\u2011", "-").split())


class AppleHTML(HTMLParser):
    """Capture just the official specs sections, or text in an HTML fragment."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections = {}
        self.text = []
        self.depth = 0
        self.active = None
        self.ignore = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.ignore += 1
        if tag == "div":
            self.depth += 1
            classes = dict(attrs).get("class", "").split()
            for section in SECTIONS:
                if "techspecs-section" in classes and section in classes:
                    self.active = (section, self.depth)
                    self.sections[section] = []

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.ignore = max(0, self.ignore - 1)
        if tag == "div":
            if self.active and self.active[1] == self.depth:
                self.active = None
            self.depth -= 1

    def handle_data(self, data):
        if not self.ignore:
            self.text.append(data)
            if self.active:
                self.sections[self.active[0]].append(data)


def plain(fragment):
    parser = AppleHTML()
    parser.feed(fragment or "")
    return normalize(" ".join(parser.text))


def parse_pages(pages):
    # The bootstrap is JavaScript wrapping a JSON value, not a JSON script.
    marker = re.search(r"productSelectionData\s*:\s*", pages["store"])
    if not marker or "PRODUCT_SELECTION_BOOTSTRAP" not in pages["store"]:
        raise ValueError("Apple store structure missing")
    data, _ = json.JSONDecoder().raw_decode(pages["store"][marker.end():])
    memory = data["configDisplayValues"][MEMORY]
    choices = sorted((key for key, value in memory.items()
                      if re.fullmatch(r"\d+(?:gb|tb)", key)
                      and isinstance(value, dict)),
                     key=lambda key: int(key[:-2]) * (1024 if key.endswith("tb") else 1))
    if not choices or not data["products"]:
        raise ValueError("Apple memory/products missing")
    memory_options = {key: {"label": plain(memory[key]["header"]),
                            "blocked": bool(memory[key].get("isBlocked", False))}
                      for key in choices}
    prices = {key: value["amount"]
              for key, value in data["mainDisplayValues"]["prices"].items()}
    if not prices or any(not isinstance(n, (int, float)) or not math.isfinite(n)
                         or not 0 < n < 200000 for n in prices.values()):
        raise ValueError("Apple prices missing or invalid")
    sections = [x for x in data["configSections"] if x["formFieldName"] == MEMORY]
    if len(sections) != 1:
        raise ValueError("Apple memory section missing")
    notice = " ".join(filter(None, (plain(sections[0].get("subHeader")),
                                   plain(sections[0].get("footer")))))
    parser = AppleHTML()
    parser.feed(pages["specs"])
    specs = {key.removeprefix("section-"): normalize(" ".join(parser.sections[key]))
             for key in SECTIONS}
    if any(len(text) < 30 for text in specs.values()) or not re.search(r"M\d+\s+Ultra", specs["chip"]):
        raise ValueError("Apple specifications missing")
    models = sorted({(x["priceKey"], x["isComingSoon"]) for x in data["products"]})
    return {"specifications": specs, "base_prices_usd": prices,
            "memory_options": memory_options, "memory_notice": notice,
            "models_coming_soon": dict(models)}


def fetch_pages():
    pages = {}
    for name, url in URLS.items():
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=35) as response:
            if response.status != 200 or not response.geturl().startswith("https://www.apple.com/"):
                raise ValueError("Unexpected Apple response")
            content = response.read(3_000_001)
            if len(content) > 3_000_000:
                raise ValueError("Apple page exceeds size bound")
            pages[name] = content.decode("utf-8")
    return pages


def fingerprint(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def describe(snapshot, previous=None, recovered=False):
    lines = ["Mac Studio watch is now running on CIRRUS." if previous is None
             else "Apple Mac Studio update" if snapshot != previous
             else "Mac Studio watch has recovered; Apple information is unchanged."]
    if previous is None:
        lines += ["Daily at 4:45 PM Eastern, even with your MacBook/app closed. "
                  "Telegram alerts only for changes or a failed check."]
    elif recovered:
        lines += ["Apple checks are working again."]
    lines += ["", snapshot["memory_notice"] or "Apple shows no memory availability notice."]
    target = snapshot["memory_options"].get("512gb")
    lines += ["512GB unified memory: " + (
        "listed in Apple's selector, marked unavailable." if target and target["blocked"]
        else "now listed in Apple's selector; exact configured price and checkout need confirmation."
        if target else "not listed in Apple's memory selector yet.")]
    lines += ["Memory choices: " + ", ".join(x.upper() for x in snapshot["memory_options"])]
    lines += ["", "U.S. starting prices by chip (not a 512GB configuration quote):"]
    for chip, price in sorted(snapshot["base_prices_usd"].items()):
        label = re.sub(r"(m\d+)(max|ultra)-(\d+)-(\d+)",
                       lambda m: f"{m[1].upper()} {m[2].title()}, {m[3]} CPU / {m[4]} GPU cores", chip)
        suffix = " (coming soon)" if snapshot["models_coming_soon"].get(chip) else ""
        lines.append(f"{label}: ${price:,.0f}{suffix}")
    for key, value in snapshot["specifications"].items():
        if previous is None or previous["specifications"].get(key) != value:
            if previous is None and key == "chip" and "Apple M5 Ultra" in value:
                value = value[value.index("Apple M5 Ultra"):]
            lines += ["", value[:800] + ("… See full specifications." if len(value) > 800 else "")]
    lines += ["", "Apple specifications: " + URLS["specs"], "Configure/order: " + URLS["store"]]
    message = "\n".join(lines)
    if len(message) > 4000:
        raise ValueError("Alert exceeds Telegram message bound")
    return message


def save_state(path, state):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=path.name + ".", delete=False) as handle:
        tmp = Path(handle.name)
        try:
            json.dump(state, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def run_once(path, fetch, send, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else {}
        now = datetime.now(timezone.utc).isoformat()
        state["last_checked_at"] = now
        try:
            snapshot = parse_pages(fetch())
        except Exception as error:
            # Exception text can contain URLs/credentials. Emit the class only.
            reason = type(error).__name__
            state["last_error"] = reason
            if not state.get("error_alerted"):
                state["error_alerted"] = bool(send(
                    "CIRRUS Mac Studio watch could not verify Apple's pages today ("
                    + reason + "). The saved baseline is unchanged; it will retry tomorrow. "
                    "This is a check failure, not an availability update."))
            save_state(path, state)
            record("macstudiowatch", False, "Apple check failed: " + reason)
            return 1, "Apple check failed: " + reason
        changed = fingerprint(snapshot) != state.get("sent_fingerprint")
        if changed or state.get("error_alerted"):
            message = describe(snapshot, state.get("sent_snapshot"), state.get("error_alerted", False))
            if not send(message):
                state["last_error"] = "Telegram delivery failed"
                save_state(path, state)
                record("macstudiowatch", False, "Telegram failed; snapshot NOT advanced")
                return 1, "Telegram failed; snapshot NOT advanced"
            state.update(sent_fingerprint=fingerprint(snapshot), sent_snapshot=snapshot,
                         last_alert_at=now, alert_count=state.get("alert_count", 0) + 1)
        state.update(last_success_at=now, last_error=None, error_alerted=False)
        save_state(path, state)
        note = "Apple change delivered to Telegram" if changed else "Apple checked; unchanged"
        record("macstudiowatch", True, note)
        return 0, note


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.status:
        state = json.loads(STATE_PATH.read_text())
        print(json.dumps({key: state.get(key) for key in (
            "last_checked_at", "last_success_at", "last_alert_at", "alert_count", "last_error")}, indent=2))
        return 0
    if not args.live:
        print(describe(parse_pages(fetch_pages())))
        return 0
    if Path(__file__).resolve().parent != PROJECT_DIR:
        parser.error("--live is restricted to the deployed CIRRUS checkout")
    from vendor_mail_watch import telegram
    from job_status import record
    try:
        code, note = run_once(STATE_PATH, fetch_pages, telegram, record)
    except Exception as error:
        note = "Watch failed: " + type(error).__name__
        record("macstudiowatch", False, note)
        code = 1
    print(note)
    return code


if __name__ == "__main__":
    sys.exit(main())
