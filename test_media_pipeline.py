"""Offline coverage, evidence and resumability tests; no live calls/files."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import media_pipeline as m


class MediaTests(unittest.TestCase):
    def test_span_coverage_and_bound(self):
        text='BEGIN '+('cross boundary facts '*7000)+' ENDING'
        spans=m.split_text(text,count=len,limit=5000)
        covered=bytearray(len(text))
        for a,b in spans:
            self.assertLessEqual(b-a,5000)
            covered[a:b]=b'1'*(b-a)
        self.assertTrue(all(covered))
        self.assertEqual(spans[0][0],0)
        self.assertEqual(spans[-1][1],len(text))

    def test_normalized_quote_returns_original(self):
        source='Exact SOURCE wording across\n  lines does not establish causation.'
        quote='exact source wording across lines does not establish causation.'
        q,a,b=m.source_quote(quote,source)
        self.assertEqual(q,source[a:b])
        with self.assertRaises(ValueError):
            m.source_quote(quote.replace('does not','does'),source)

    def test_bad_json_is_failure_not_no_claims(self):
        for bad in ('not json','{"claims": "none"}','{"claims":[{"claim":"guess"}]}'):
            with self.assertRaises((ValueError,TypeError)):
                m.parse_claims(bad,'some source')
        self.assertEqual(m.parse_claims('{"claims":[]}','source'),[])

    def test_partial_failure_resumes_without_skipping_end(self):
        text='START '+('middle '*4000)+' UNIQUE ENDING'
        calls=[]
        def caller(system,part,task):
            calls.append(part)
            if len(calls)==2:
                raise RuntimeError('temporary model failure')
            return 'notes '+('UNIQUE ENDING' if 'UNIQUE ENDING' in part else 'START')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                m.analyze(text,'summarize',root=tmp,caller=caller,counter=len)
            state=json.loads(next(Path(tmp).rglob('coverage.json')).read_text())
            self.assertFalse(state['complete'])
            first=calls[0]
            before=len(calls)
            result=m.analyze(text,'summarize',root=tmp,caller=caller,counter=len)
            self.assertNotIn(first,calls[before:])
            self.assertIn('UNIQUE ENDING',result)
            state=json.loads(next(Path(tmp).rglob('coverage.json')).read_text())
            self.assertTrue(state['complete'])
            self.assertEqual(state['completed'],state['sections'])
            self.assertEqual(next(Path(tmp).rglob('transcript.txt')).read_text(),text)

    def test_context_limit_fails_before_completion(self):
        with patch.object(m,'token_count',return_value=40000),patch.object(m,'request') as req:
            with self.assertRaises(ValueError):m.complete('instructions','text','test')
            req.assert_not_called()

    def test_no_domain_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                m.analyze('test','task',domain='../bad',root=tmp)

    def test_published_claim_is_source_not_model_guess(self):
        source='A model with 27 billion parameters used 29 GiB of memory in this test.'
        def caller(*args):
            return json.dumps({'claims':[{'claim':'An invented model name had this result',
                'why_it_applies':'This proves our system is identical',
                'how_to_test':'Compare a local model under the same conditions','quote':source}]})
        with tempfile.TemporaryDirectory() as tmp:
            result=m.analyze(source,'hardware','youtube-hardware',True,root=tmp,
                             caller=caller,counter=len,metadata={'title':'Test episode'})
            self.assertEqual(result[0]['claim'],'Presenter reports: '+source)
            self.assertNotIn('identical',result[0]['why_it_applies'])
            self.assertTrue(next(Path(tmp).rglob('metadata.json')).exists())

    def test_weekly_fetch_keeps_entire_transcript(self):
        import types
        entry=types.SimpleNamespace()
        class Entry(dict):
            __getattr__=dict.__getitem__
        entry=Entry(title='Episode',enclosures=[{'type':'audio/mpeg','href':'https://example.org/audio'}])
        feed=types.SimpleNamespace(entries=[entry])
        fake_feed=types.SimpleNamespace(parse=lambda data:feed)
        response=types.SimpleNamespace(content=b'<rss/>',raise_for_status=lambda:None)
        fake_requests=types.SimpleNamespace(get=lambda *a,**kw:response)
        full='Beginning '+('middle '*6000)+' ENDING'
        with patch.dict('sys.modules',{'feedparser':fake_feed,'requests':fake_requests}), \
             patch.object(m,'transcribe',return_value=full):
            rows=m.weekly_fetch({'days_back':7,'podcasts':[{'name':'Test','rss':'https://example.org/feed'}]})
        self.assertTrue(rows[0]['content'].endswith('ENDING'))
        self.assertEqual(rows[0]['content'],'[TRANSCRIBED]\n'+full)

    def test_remote_failure_is_not_local_fallback(self):
        with patch.object(m.socket,'gethostname',return_value='cirrus'), \
             patch.object(m.subprocess,'run') as run:
            run.return_value.returncode=1
            run.return_value.stderr='worker unavailable'
            with self.assertRaisesRegex(RuntimeError,'Cumulus_media_worker_failed'):
                m.call('analyze',text='full text',instructions='test')


if __name__=='__main__':unittest.main()
