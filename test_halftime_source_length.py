"""The routing sweep must read each source beyond the digest's short excerpt."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
import halftime_routing as r

GAME = {"date": "2026-11-01", "opponent": "Test", "week": 8}
EVENT = {"artist": "Late Page Act", "date": "2026-11-01",
         "venue": "Test Arena", "city": "Pittsburgh, PA", "style": ""}


class SourceLengthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(r, "LOG_PATH", Path(self.tmp.name) / "routing.log"),
                  patch.object(r, "METROS", [("Pittsburgh, PA", 0)])):
            p.start()
            self.addCleanup(p.stop)

    def test_default_fetch_reads_past_3000_and_each_source_reaches_model(self):
        seen = []
        def fetch(url, **kw):
            seen.append(kw["max_chars"])
            return ("x" * 9500 + " LATE_EVENT " + "z" * 6000)[:kw["max_chars"]], False
        daily = types.SimpleNamespace(fetch_article_content=fetch)
        blocks = []
        def extract(block):
            blocks.append(block)
            return [dict(EVENT, artist="Act " + str(len(blocks)))] if "LATE_EVENT" in block else []
        with patch.dict(sys.modules, {"cirrus_daily": daily}):
            result = r.sweep_game(GAME, {}, searcher=lambda q: ["a", "b", "c"], extractor=extract)
        self.assertEqual(seen, [12001] * 3)
        self.assertEqual(len(blocks), 3)
        self.assertTrue(all(b.count("SOURCE:") == 1 for b in blocks))
        self.assertEqual(len(result["events"]), 3)
        row = result["coverage"][0]
        self.assertEqual(row["chars_read"], 36000)
        self.assertEqual(row["capped_sources"], 3)
        self.assertIsNone(row["error"])

    def test_duplicate_urls_and_shows_do_not_multiply_but_distinct_shows_survive(self):
        fetched = []
        def fetch(url):
            fetched.append(url)
            return url
        def extract(block):
            if block.endswith("a"):
                return [EVENT]
            return [dict(EVENT, style="rock"), dict(EVENT, venue="Other Arena")]
        result = r.sweep_game(GAME, {}, searcher=lambda q: ["a", "a", "b"],
                              fetcher=fetch, extractor=extract)
        self.assertEqual(fetched, ["a", "b"])
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(result["events"][0]["style"], "rock")
        self.assertEqual(result["events"][0]["source_url"], "a")

    def test_partial_extraction_retains_good_events_and_marks_home_failed(self):
        calls = []
        def extract(block, creds, stats, system=None):
            calls.append(system)
            return None if block.endswith("bad") else [EVENT]
        out = Path(self.tmp.name) / "routing.json"
        with patch.object(r, "_extract", side_effect=extract):
            result = r.run(games=[GAME], creds={}, out_path=out,
                           searcher=lambda q: ["good", "bad"], fetcher=lambda u: u)
        row = next(iter(json.loads(out.read_text())["games"].values()))
        self.assertEqual(len(row["events"]), 1)
        self.assertIn("partial", row["coverage"][0]["error"])
        self.assertEqual(row["coverage"][0]["extraction_failed"], 1)
        self.assertEqual(len(result["home_failed"]), 1)
        self.assertTrue(all("2026-10-29 through 2026-11-04" in p for p in calls))

    def test_empty_answer_is_successful_coverage_and_window_is_enforced(self):
        for answer in ([], [dict(EVENT, date="2026-11-20")]):
            result = r.sweep_game(GAME, {}, searcher=lambda q: ["a"],
                                  fetcher=lambda u: u, extractor=lambda b: answer)
            self.assertEqual(result["events"], [])
            self.assertIsNone(result["coverage"][0]["error"])

    def test_all_failed_extractions_are_not_empty_success(self):
        result = r.sweep_game(GAME, {}, searcher=lambda q: ["a", "b"],
                              fetcher=lambda u: u, extractor=lambda b: None)
        self.assertEqual(result["events"], [])
        self.assertEqual(result["coverage"][0]["error"], "extraction unusable")
        self.assertEqual(result["coverage"][0]["extraction_failed"], 2)


if __name__ == "__main__":
    unittest.main()
