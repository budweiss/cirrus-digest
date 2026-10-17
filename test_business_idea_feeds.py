import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import business_idea_feeds as F

class FeedTests(unittest.TestCase):
    def test_skips_stale_first_advertisement_and_resolves_redirect_relative_link(self):
        page = SimpleNamespace(status_code=200, url='https://publisher.test/blog/', text='''
<link rel="alternate" type="application/rss+xml" href="stale.xml">
<link rel="alternate" type="application/atom+xml" href="current.xml?a=1&amp;b=2">''')
        good='https://publisher.test/blog/current.xml?a=1&b=2'
        with patch.dict('sys.modules', {'requests': SimpleNamespace(get=lambda *a, **k: page)}), patch.object(F,'verify_feed',side_effect=lambda url,**_: (url==good,'test')) as verify:
            self.assertEqual(F.find_feed_url('https://old.test'),good)
            self.assertEqual(verify.call_count,2)

    def test_dead_advertised_feed_does_not_prevent_verified_common_path(self):
        page=SimpleNamespace(status_code=200,url='https://publisher.test',text='<link rel="alternate" type="application/rss+xml" href="/dead">')
        good='https://publisher.test/latest?format=rss'
        with patch.dict('sys.modules', {'requests': SimpleNamespace(get=lambda *a, **k: page)}),patch.object(F,'verify_feed',side_effect=lambda url,**_: (url==good,'test')):
            self.assertEqual(F.find_feed_url(page.url),good)
        with patch.dict('sys.modules', {'requests': SimpleNamespace(get=lambda *a, **k: page)}),patch.object(F,'verify_feed',return_value=(False,'stale')):
            self.assertEqual(F.find_feed_url(page.url),'')

    def test_discovery_rejects_known_aliases_and_only_persists_live_feeds(self):
        state={'trial':[],'promoted':[],'retired':[{'name':'Old','rss':'https://old.test/','site':'https://www.old.test/'}]}
        proposals=[{'name':'Again','site':'http://old.test','why':'test'},
                   {'name':'Dead','site':'https://dead.test','why':'test'},
                   {'name':'Good','site':'https://good.test','why':'test'},
                   {'name':'Duplicate','site':'https://www.good.test/','why':'test'}]
        def verify(url,**_):return (url=='https://good.test/rss','test')
        with patch.object(F,'_load',return_value=copy.deepcopy(state)),patch.object(F,'_save') as save, \
             patch.object(F,'all_sources',return_value=[]),patch.object(F,'propose',return_value=proposals), \
             patch.object(F,'verify_feed',side_effect=verify),patch.object(F,'find_feed_url',side_effect=lambda site: 'https://good.test/rss' if site=='https://good.test' else '') as find:
            result=F.discover(4)
        self.assertEqual(len(result['added_to_trial']),1)
        self.assertEqual(len(result['rejected']),3)
        self.assertEqual([c.args[0] for c in find.call_args_list],['https://dead.test','https://good.test'])
        self.assertEqual(save.call_args.args[0]['trial'][0]['site'],'https://good.test')
        self.assertEqual(save.call_args.args[0]['trial'][0]['rss'],'https://good.test/rss')

if __name__=='__main__':unittest.main()
