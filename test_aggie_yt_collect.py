"""Offline regression checks for source review, channel fairness and retries."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import aggie_yt_collect as c
import aggie_yt_digest as d


class CollectionTests(unittest.TestCase):
    def test_unsupported_evidence_and_copied_passage_refused(self):
        claims = [{'quote': 'one two three four five six seven eight nine ten eleven twelve thirteen'}]
        for item in [{'text': 'A concise paraphrase.', 'evidence': [1]},
                     {'text': claims[0]['quote'], 'evidence': [0]}]:
            with self.assertRaises(ValueError):
                c.validate_bullets({'bullets': [item]}, claims)

    def test_negative_review_never_becomes_approved(self):
        claims = [{'quote': 'The presenter says this is uncertain, not a guaranteed result.'}]
        caller = Mock(side_effect=[
            '{"bullets":[{"text":"The outcome is guaranteed.","evidence":[0]}]}',
            '{"approved":false,"problems":["lost uncertainty"]}',
            '{"bullets":[{"text":"It is guaranteed.","evidence":[0]}]}',
            '{"approved":false,"problems":["still unsupported"]}'])
        with self.assertRaisesRegex(ValueError, 'review_failed'):
            c.summarize(claims, caller)
        self.assertEqual(caller.call_count, 4)

    def test_reviewed_summary_keeps_evidence(self):
        caller = Mock(side_effect=[
            '{"bullets":[{"text":"The presenter sees a possible decline, with uncertainty.","evidence":[0]}]}',
            '{"approved":true,"problems":[]}'])
        bullets, review = c.summarize([{'quote': 'We might see prices fall, but there are no guarantees.'}], caller)
        self.assertEqual(len(bullets), 1)
        self.assertTrue(review['approved'])
        self.assertEqual(review['bullets'][0]['evidence'], [0])

    def test_failed_channel_does_not_starve_other_channels(self):
        channels = [{'name': 'A', 'channel_id': 'a'}, {'name': 'B', 'channel_id': 'b'}]
        video = {'video_id': 'example0001', 'published': '2026-10-14', 'url': 'https://www.youtube.com/watch?v=example0001'}
        result = {'video': video, 'version': c.VERSION, 'bullets': ['A reviewed summary.'], 'review': {'approved': True}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(c, 'SUMMARIES', Path(tmp)), \
                patch.object(d, 'aggie_channels', return_value=channels), \
                patch.object(d, 'newest_video', side_effect=[RuntimeError('feed unavailable'), video]), \
                patch.object(c.media, 'call', return_value=result), patch.object(c.time, 'sleep'), \
                patch('job_status.record') as record, patch('vendor_mail_watch.telegram', return_value=True):
            self.assertEqual(c.collect(), 1)
            self.assertEqual(json.loads((Path(tmp) / 'example0001.json').read_text())['channel'], 'B')
            self.assertFalse(record.call_args.args[1])
            self.assertEqual(json.loads((Path(tmp) / 'collection.json').read_text())['ready'], 1)

    def test_unreviewed_output_cannot_be_delivered(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(d, 'PROJECT_DIR', Path(tmp)):
            folder = Path(tmp) / 'yt-watch/aggie-summaries'; folder.mkdir(parents=True)
            data = {'channel': 'A', 'video': {'url': 'https://example.invalid/video'}, 'bullets': ['Unsupported.'],
                    'review': {'approved': False}}
            (folder/'bad.json').write_text(json.dumps(data))
            self.assertEqual(d.reviewed_summaries(), {})


if __name__ == '__main__':
    unittest.main()
