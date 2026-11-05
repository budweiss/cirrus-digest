"""Public synthetic fixtures; no model, credentials, inbox or network access."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import media_pipeline as media
import business_idea_scan as business
import creator_backfill
import runtime_window


class CreatorReviewTests(unittest.TestCase):
    def test_authorized_catchup_is_not_a_morning_producer(self):
        schedule=runtime_window.parse_schedule('===== CIRRUS (launchd) =====\n'
            '  com.cirrus.morningbrief  07:30\n===== CUMULUS (systemd timers) =====\n'
            '  cirrus-snowguard-catchup.timer  Mon 11:00:00\n')
        self.assertEqual(runtime_window.audit(schedule)[0],[])
        schedule[-1]['job']='unapproved-producer.timer'
        self.assertTrue(runtime_window.audit(schedule)[0])
    def test_business_domain_and_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            result = media.analyze('A public creator source.', media.youtube_instructions('business'),
                domain='youtube-business', claims=True, root=folder,
                caller=lambda *args: '{"claims":[]}', counter=len)
        self.assertEqual(result, [])
        self.assertIn('daily human labor', media.youtube_instructions('business'))
        self.assertIn('refunds', media.youtube_instructions('business'))
        self.assertIn('MacBook', media.youtube_instructions('hardware'))

    def test_explicit_backfill_avoids_rss_and_exports_business_evidence(self):
        import types
        import yt_watch
        quote='The experiment sold a digital workbook; all customers were refunded.'
        class Captions(list):
            def to_raw_data(self): return [{'text':quote,'start':0,'duration':1}]
        fake=types.SimpleNamespace(YouTubeTranscriptApi=lambda:types.SimpleNamespace(
            fetch=lambda *a,**kw:Captions([types.SimpleNamespace(text=quote)])))
        payload=dict(channels=[dict(name='Mark Tilbury',channel_id='CID',lane='business')],
            videos={'CID':[dict(video_id='abcdefghijk',title='Test',published='2026-11-05',url='u')]},
            seen={'video_ids':[]},limit=1)
        with tempfile.TemporaryDirectory() as folder,patch.object(media,'ROOT',Path(folder)), \
                patch.dict('sys.modules',{'youtube_transcript_api':fake}), \
                patch.object(media,'analyze',return_value=[dict(claim=quote,quote=quote,
                    why_it_applies='test',how_to_test='test')]), \
                patch.object(yt_watch,'fetch_feed',side_effect=AssertionError('RSS must not run')):
            result=media.youtube(payload)
        self.assertEqual(result['seen']['video_ids'],['abcdefghijk'])
        self.assertIn('refunded',result['business_items'][0]['content'])

    def test_status_never_calls_missing_source_reviewed(self):
        result=creator_backfill.status({'videos':[{'video_id':'a'},{'video_id':'b'}]},
            {'items':{'a':{'status':'source_unavailable'}}})
        self.assertEqual(result['counts'],{'source_unavailable':1,'pending':1})

    def test_business_intake_dedup_and_failure_preserves_work(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'video.json'
            row = dict(video_id='abcdefghijk',title='Public experiment',source='YouTube',content='Evidence')
            path.write_text(json.dumps(row))
            stats = dict(fetched=0,fresh=0)
            with patch.object(business,'_score_and_store') as score:
                done = business.ingest_youtube_reviews({},stats,set(),inbox=folder)
                self.assertEqual(len(done),1)
                business.ingest_youtube_reviews({},stats,done,inbox=folder)
                self.assertEqual(score.call_count,1)
            with patch.object(business,'_score_and_store',side_effect=RuntimeError('temporary')):
                with self.assertRaises(RuntimeError):
                    business.ingest_youtube_reviews({},stats,set(),inbox=folder)
            self.assertTrue(path.exists())
            with patch.object(business,'_score_and_store') as score:
                self.assertEqual(business.ingest_youtube_reviews({},stats,set(),inbox=folder,dry_run=True),set())
                score.assert_not_called()

    def test_seen_files_do_not_starve_new_intake(self):
        with tempfile.TemporaryDirectory() as folder:
            seen=set()
            for i in range(14):
                vid='%011d'%i
                (Path(folder)/('%02d.json'%i)).write_text(json.dumps(dict(video_id=vid,
                    title='Public source',source='YouTube',content='Evidence')))
                if i<13: seen.add('https://www.youtube.com/watch?v='+vid)
            with patch.object(business,'_score_and_store') as score:
                done=business.ingest_youtube_reviews({},dict(fetched=0,fresh=0),seen,inbox=folder)
            self.assertEqual(score.call_count,1)
            self.assertEqual(len(done),1)


if __name__=='__main__':
    unittest.main()
