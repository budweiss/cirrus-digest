"""S346 scratch integration: real worker/adapter, synthetic captions/model, no sends."""
import contextlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import media_jobs as jobs
import media_pipeline as media
import yt_watch as yt

A, B, C = 'AAAAAAAAAAA', 'BBBBBBBBBBB', 'CCCCCCCCCCC'

class RetryScenarios(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.seen = self.root / 'caller-seen.json'
        self.seen.write_text(json.dumps({'video_ids': ['prior-history']}))
        self.out = self.root / 'findings'
        self.videos = [A, B]
        self.failed = set()
        self.analyzed = []
        self.original_run = yt.run
        for name, value in [('SEEN_PATH', self.seen), ('OUT_DIR', self.out), ('run', self.original_run)]:
            self.stack.enter_context(patch.object(yt, name, value))
        self.stack.enter_context(patch.object(yt, 'log'))
        self.stack.enter_context(patch.object(yt, 'load_channels', return_value=[{'name':'Fixture','channel_id':'fixture','lane':'hardware'}]))
        self.stack.enter_context(patch.object(yt, 'fetch_feed', side_effect=self.feed))
        self.stack.enter_context(patch.object(yt.time, 'sleep'))
        self.stack.enter_context(patch.object(yt, 'main', side_effect=lambda: yt.run()))
        self.stack.enter_context(patch.object(media, 'ROOT', self.root))
        self.stack.enter_context(patch.object(media, 'analyze', side_effect=self.analyze))
        class CaptionList(list):
            def to_raw_data(self): return []
        api = types.SimpleNamespace(fetch=lambda *a, **kw: CaptionList([types.SimpleNamespace(text='Synthetic source transcript')]))
        self.stack.enter_context(patch.dict(sys.modules, {'youtube_transcript_api':types.SimpleNamespace(YouTubeTranscriptApi=lambda:api)}))
        self.stack.enter_context(patch.object(media, 'call', side_effect=lambda action, **kw:media.youtube(kw)))
        self.stack.enter_context(patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch.object(subprocess, 'Popen', side_effect=AssertionError('subprocess forbidden')))

    def feed(self, channel):
        return [dict(video_id=v,title='Video '+v,published='2026-09-28',url='https://example.invalid/'+v) for v in self.videos]

    def analyze(self, text, instructions, **kw):
        vid = kw['metadata']['video_id']; self.analyzed.append(vid)
        if vid in self.failed: raise RuntimeError('synthetic model failure')
        return [{'claim':'claim-'+vid,'why_it_applies':'fixture','how_to_test':'fixture'}]

    def run_batch(self):
        # The production entry mutates yt.run for its process lifetime; each test
        # invocation represents a fresh scheduled process.
        yt.run = self.original_run
        return jobs.youtube()

    def body(self): return next(self.out.glob('*.md')).read_text()
    def cursor(self): return set(json.loads(self.seen.read_text())['video_ids'])

    def test_partial_model_failure_retries_only_failed_video(self):
        self.failed = {B}
        first = self.run_batch()
        self.assertEqual(first['extract_errors'], 1)
        self.assertEqual(self.cursor(), {'prior-history', A})
        self.assertEqual(self.body().count('claim-'+A), 1)
        self.failed.clear(); self.analyzed.clear()
        second = self.run_batch()
        self.assertEqual(second['extract_errors'], 0)
        self.assertEqual(self.analyzed, [B])
        self.assertEqual(self.cursor(), {'prior-history', A, B})
        self.assertEqual(self.body().count('claim-'+A), 1)
        self.assertEqual(self.body().count('claim-'+B), 1)
        self.assertIn('extraction FAILED', self.body())  # historical attempt retained

    def test_completed_replay_does_not_duplicate_or_reanalyze(self):
        self.run_batch(); before = self.body(); self.analyzed.clear()
        result = self.run_batch()
        self.assertEqual(result['processed'], 0)
        self.assertEqual(self.analyzed, [])
        self.assertEqual(self.body(), before)

    def fail_caller_cursor_once(self):
        original = media.atomic_json
        def save(path, data):
            if Path(path) == self.seen: raise OSError('synthetic caller cursor write failure')
            return original(path, data)
        with patch.object(media, 'atomic_json', side_effect=save):
            with self.assertRaisesRegex(OSError, 'caller cursor write failure'):self.run_batch()
        self.assertEqual(self.cursor(), {'prior-history'})

    def test_same_batch_after_cursor_write_failure_is_idempotent(self):
        self.fail_caller_cursor_once(); before = self.body()
        self.run_batch()
        self.assertEqual(self.body(), before)
        self.assertEqual(self.cursor(), {'prior-history', A, B})

    def test_changed_batch_after_cursor_write_failure_no_duplicate_claims(self):
        self.fail_caller_cursor_once()
        self.videos.append(C)
        self.run_batch()
        self.assertEqual(self.cursor(), {'prior-history', A, B, C})
        for vid in (A, B, C):self.assertEqual(self.body().count('claim-'+vid), 1, vid)

    def test_changed_evidence_and_multiple_claims_are_preserved(self):
        def render(claims):
            video = self.feed('fixture')[0]
            return yt.render([dict(video,channel='Fixture',lane='hardware',
                claims=[dict(claim=c,why_it_applies='why',how_to_test='test') for c in claims])], '2026-09-28')
        first = render(['first claim', 'second claim'])
        changed = render(['first claim', 'changed second claim'])
        merged = jobs.merge_findings(jobs.merge_findings('', first), changed)
        self.assertIn('**second claim**', merged)
        self.assertIn('**changed second claim**', merged)
        self.assertEqual(jobs.merge_findings(merged, first), merged)
        self.assertEqual(jobs.merge_findings(merged, changed), merged)

    def test_three_overlapping_batches_deduplicate_every_video(self):
        responses=[]
        for video_ids in ([A], [A,B], [A,B,C]):
            rows=[dict(video_id=v,title='Video '+v,published='2026-09-28',
                url='https://example.invalid/'+v,channel='Fixture',lane='hardware',
                claims=[dict(claim='claim-'+v,why_it_applies='',how_to_test='')]) for v in video_ids]
            responses.append(yt.render(rows,'2026-09-28'))
        merged=''
        for response in responses: merged=jobs.merge_findings(merged,response)
        for vid in (A,B,C): self.assertEqual(merged.count('claim-'+vid),1)
        for response in responses:self.assertEqual(jobs.merge_findings(merged,response),merged)

if __name__ == '__main__': unittest.main()
