import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import shore_schedule as S

TEXT='123 Example Street, Wildwood — asking sale price $300,000; active 2026-10-14.'
SOURCE={'text':TEXT,'url':'https://example.com/listing','title':'Synthetic fixture'}
ROW={'town':'Wildwood','address':'123 Example Street','price':300000,'price_kind':'asking','status':'Active 2026-10-14','quote':TEXT,'summary':'Synthetic property with no confirmed rental or condition evidence.'}

class ShoreChecks(unittest.TestCase):
    def test_blocked_subscription_excerpt_is_an_access_gap(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        fetch=Mock(return_value=('Blocked subscription excerpt '*40,True))
        module=SimpleNamespace(search_web=lambda *a,**k:[],fetch_article_content=fetch)
        with patch.dict('sys.modules',{'cirrus_daily':module}),patch.object(S,'COUNTIES',(('Synthetic county','https://example.com/blocked'),)):
            with self.assertRaisesRegex(RuntimeError,'shore_sources_unavailable'):S.gather()
        self.assertEqual(fetch.call_args.kwargs['max_chars'],16000)

    def test_twice_covered_unchanged_property_waits_for_a_change(self):
        history=S.record_coverage([ROW],ROW['address'],'2026-10-10',{})
        history=S.record_coverage([ROW],ROW['address'],'2026-10-13',history)
        self.assertEqual(S.new_or_changed([ROW],history),[])
        changed=dict(ROW,price=280000)
        self.assertEqual(S.new_or_changed([changed],history),[changed])

    def test_newsletter_links_drop_tracking_and_other_mail(self):
        from datetime import date
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)
            (folder/'20261014-example.md').write_text('> From: `news@news.pressofatlanticcity.com`\nPrivate synthetic subject/body\nhttps://pressofatlanticcity.com/news/example.html?tracking=private#part\nhttps://unrelated.example/mail')
            (folder/'20261014-unrelated.md').write_text('> From: `other@example.com`\nhttps://pressofatlanticcity.com/news/other.html')
            result=S.newsletter_links(folder,date(2026,10,14))
            self.assertEqual(result['issues'],1)
            self.assertEqual(result['urls'],['https://pressofatlanticcity.com/news/example.html'])
            self.assertNotIn('private',json.dumps(result).lower())

    def test_atlantic_city_excluded(self):
        row=dict(ROW,town='Atlantic City')
        with self.assertRaisesRegex(ValueError,'town_out_of_scope'):S.validate_candidates({'properties':[row]},SOURCE)

    def test_budget_and_upset_are_distinct(self):
        with self.assertRaisesRegex(ValueError,'outside_price_scope'):S.validate_candidates({'properties':[dict(ROW,price=550000)]},SOURCE)
        self.assertEqual(len(S.validate_candidates({'properties':[dict(ROW,town='Villas',price=550000,price_kind='upset')]},SOURCE)),1)

    def test_exact_source_required(self):
        with self.assertRaisesRegex(ValueError,'unmatched_property_evidence'):
            S.validate_candidates({'properties':[dict(ROW,quote='This source never said anything about this property.')]},SOURCE)

    def test_empty_page_is_not_invented_inventory(self):
        responses=iter([json.dumps({'properties':[],'coverage':'Page shell; inventory not available.'}),'{"approved":true,"problems":[]}'])
        rows,coverage=S.extract(SOURCE,lambda _:next(responses))
        self.assertEqual(rows,[]);self.assertIn('not available',coverage)

    def test_independent_rejection_withholds(self):
        responses=iter([json.dumps({'properties':[ROW]}),'{"approved":false,"problems":["wrong price"]}'])
        with self.assertRaisesRegex(ValueError,'property_review_failed'):S.extract(SOURCE,lambda _:next(responses))

    def test_uncertain_delivery_never_sends_again(self):
        import io,shore_digest_send as sender
        date=S.datetime.now(S.NY).date().isoformat()
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);creds=folder/'credentials.json';creds.write_text(json.dumps({'outlook_email':'synthetic@example.com','outlook_password':'not-a-real-credential'}))
            (folder/(date+'.receipt.json')).write_text(json.dumps({'status':'sending'}))
            with patch.object(S,'STATE',folder),patch.object(S.sys,'stdin',io.StringIO(json.dumps({'date':date,'markdown':'# Jersey Shore Deal Digest — '+date+'\nSynthetic preview'}))),patch.object(sender,'CREDS_PATH',creds),patch.object(sender,'send_live') as send:
                with self.assertRaisesRegex(ValueError,'shore_delivery_outcome_uncertain'):S.worker('live-send')
            send.assert_not_called()

if __name__=='__main__':unittest.main()
