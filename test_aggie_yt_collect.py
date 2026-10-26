"""Offline regression checks for source review, channel fairness and retries."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, call, patch

import aggie_yt_collect as c
import aggie_yt_digest as d


class CollectionTests(unittest.TestCase):
    def setUp(self):
        # A renamed feed helper must not turn an offline fixture into HTTP.
        for guard in (patch.object(d.urllib.request, 'urlopen', side_effect=AssertionError('offline collection test')),
                      patch.object(d, 'log')):
            guard.start()
            self.addCleanup(guard.stop)

    def test_unsupported_evidence_and_copied_passage_refused(self):
        claims = [{'quote': 'one two three four five six seven eight nine ten eleven twelve thirteen'}]
        for item in [{'text': 'A concise paraphrase.', 'evidence': [1]},
                     {'text': claims[0]['quote'], 'evidence': [0]}]:
            with self.assertRaises(ValueError):
                c.validate_bullets({'bullets': [item]}, claims)

    def test_drafting_reserves_alleges_for_accusations(self):
        # S497: the S471 wording made drafts call ordinary market data "alleged";
        # the unchanged reviewer rejected that every night from 10-21. The
        # candidate below was verified against the live reviewer before shipping.
        seen = {}
        def caller(system, text, task):
            seen.setdefault(task, system)
            if task.endswith('summary'):
                return json.dumps({'bullets': [{'text': 'The presenter says the market may rise.', 'evidence': [0]}]})
            return json.dumps({'approved': True, 'problems': []})
        c.summarize([{'quote': 'I think the market could go higher from here into next year.'}], caller=caller)
        system = seen['media:aggie:summary']
        self.assertIn('neutral verb', system)
        self.assertIn('use "alleges" only for an accusation', system)
        self.assertNotIn('including religious predictions and allegations', system)
        # Buddy 2026-10-26: prophecy is left out of Aggie's summaries entirely.
        self.assertIn('Leave out religious or prophetic predictions entirely', system)

    def test_rejection_keeps_every_draft_and_finding(self):
        # S497 P4(a): a rejected summary used to leave no trace at all.
        problems = {'approved': False, 'problems': ['Bullet 1 states a prediction as fact.']}
        def caller(system, text, task):
            if task.endswith('summary'):
                return json.dumps({'bullets': [{'text': 'The presenter says the market may rise.', 'evidence': [0]}]})
            return json.dumps(problems)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'vid.rejected.json'
            with self.assertRaisesRegex(ValueError, '^aggie_summary_review_failed$'):   # same error code
                c.summarize([{'quote': 'I think the market could go higher from here into next year.'}],
                            caller=caller, reject_path=path)
            saved = json.loads(path.read_text())
            self.assertEqual(len(saved['attempts']), 2)
            self.assertEqual(saved['attempts'][1]['review'], problems)
            self.assertIn('market may rise', saved['attempts'][0]['draft'])
            self.assertEqual(saved['evidence'][0]['id'], 0)

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
        self.assertIn("The outcome is guaranteed.",caller.call_args_list[2].args[1])
        self.assertIn("lost uncertainty",caller.call_args_list[2].args[1])

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
                patch.object(d, 'feed_videos', side_effect=[RuntimeError('feed unavailable'), [video]]) as feed, \
                patch.object(c.media, 'call', return_value=result), patch.object(c.time, 'sleep'), \
                patch('job_status.record') as record, patch('vendor_mail_watch.telegram', return_value=True):
            self.assertEqual(c.collect(), 1)
            self.assertEqual(feed.call_args_list, [call('a', count=1), call('b', count=1)])
            self.assertEqual(json.loads((Path(tmp) / 'example0001.json').read_text())['channel'], 'B')
            self.assertFalse(record.call_args.args[1])
            self.assertEqual(json.loads((Path(tmp) / 'collection.json').read_text())['ready'], 1)
            # S423: the pending log carries the error string, not just the type
            # (the MOTM RuntimeError's real cause was in the message, not the name).
            state = json.loads((Path(tmp) / 'collection.json').read_text())
            self.assertEqual(state['errors'], ['A: RuntimeError: feed unavailable'])

    def test_depth_reuses_reviewed_cache_and_gates_each_new_video(self):
        videos = [{'video_id': name, 'published': '2026-10-15', 'url': 'https://example.invalid/' + name}
                  for name in ('cached00001', 'newvideo001')]
        for approved in (True, False):
            with self.subTest(approved=approved), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(c, 'SUMMARIES', Path(tmp)), \
                    patch.object(d, 'aggie_channels', return_value=[{'name': 'A', 'channel_id': 'a'}]), \
                    patch.object(d, 'feed_videos', return_value=videos) as feed, \
                    patch.object(c.media, 'call', return_value={'video': videos[1], 'version': c.VERSION,
                                 'bullets': ['Fixture summary.'], 'review': {'approved': approved}}) as model, \
                    patch('job_status.record') as record, patch('vendor_mail_watch.telegram', return_value=True):
                (Path(tmp) / 'cached00001.json').write_text(json.dumps({
                    'video': videos[0], 'version': c.VERSION, 'bullets': ['Cached fixture.'],
                    'review': {'approved': True}}))
                self.assertEqual(c.collect(depth=2), 0 if approved else 1)
                feed.assert_called_once_with('a', count=2)
                model.assert_called_once_with('aggie-summary', video=videos[1])
                self.assertEqual((Path(tmp) / 'newvideo001.json').exists(), approved)
                self.assertEqual(record.call_args.args[1], approved)
                state = json.loads((Path(tmp) / 'collection.json').read_text())
                self.assertEqual(state['ready'], 2 if approved else 1)

    def test_unreviewed_output_cannot_be_delivered(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(d, 'PROJECT_DIR', Path(tmp)):
            folder = Path(tmp) / 'yt-watch/aggie-summaries'; folder.mkdir(parents=True)
            data = {'channel': 'A', 'video': {'url': 'https://example.invalid/video'}, 'bullets': ['Unsupported.'],
                    'review': {'approved': False}}
            (folder/'bad.json').write_text(json.dumps(data))
            self.assertEqual(d.reviewed_summaries(), {})


if __name__ == '__main__':
    unittest.main()
