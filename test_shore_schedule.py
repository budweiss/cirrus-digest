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
        module=SimpleNamespace(search_web=lambda *a,**k:['https://example.com/blocked'],fetch_article_content=fetch)
        with patch.dict('sys.modules',{'cirrus_daily':module}),patch.object(S,'COUNTIES',()):
            with self.assertRaisesRegex(RuntimeError,'shore_sources_unavailable'):S.gather()
        self.assertEqual(fetch.call_args.kwargs['max_chars'],16000)

    def test_pdf_amounts_cannot_cross_case_boundaries(self):
        layout='OCEAN COUNTY UPSET\nREAL ESTATE LISTING FOR 10/20/2026 AMOUNT\n CH 100\n PLAINTIFF $850,000.00\n SEQ 001 1 EXAMPLE ST TOMS RIVER NJ\n CH 200\n PLAINTIFF $350,000.00 ADJOURNED UNTIL 11/17/2026\n SEQ 001 2 EXAMPLE ST SEASIDE PARK NJ\n'
        blocks,total=S.ocean_blocks(layout)
        self.assertEqual(total,2);self.assertEqual(len(blocks),1)
        self.assertIn('$350,000.00',blocks[0]);self.assertNotIn('$850,000.00',blocks[0])
        self.assertIn('ADJOURNED UNTIL',blocks[0]);self.assertIn('UPSET AMOUNT',blocks[0])
        with self.assertRaisesRegex(ValueError,'amount_alignment'):
            S.ocean_blocks(layout.replace('$850,000.00',''))

    def test_county_form_table_and_details_use_one_session(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        index='<h2>Cape May County, NJ</h2><form><table><tr><td><a href="/Sales/SaleDetails?PropertyId=1">Details</a></td><td>10/20/2026</td><td>123 Example Street Wildwood NJ</td></tr></table></form>'
        detail='<form>Sales Listing Detail (Cape May County, NJ) Property Address: 123 Example Street Wildwood NJ Minimum Bid: $300,000.00</form>'
        response=lambda text,url:SimpleNamespace(text=text,url=url,raise_for_status=lambda:None)
        session=Mock();session.get.side_effect=[response(index,'https://salesweb.civilview.com/Sales/SalesSearch?countyId=52'),response(detail,'https://salesweb.civilview.com/Sales/SaleDetails?PropertyId=1')]
        with patch('requests.Session',return_value=session):sources,gaps=S.county_sources('Cape May','https://salesweb.civilview.com/Sales/SalesSearch?countyId=52')
        self.assertEqual(session.get.call_count,2)
        self.assertIn('$300,000.00',sources[0]['text']);self.assertIn('read 1 index rows',gaps[-1])
        self.assertIn('attempted 1 detail pages',gaps[-1])
        session.get.side_effect=[response(index.replace('Cape May','Monmouth'),'https://salesweb.civilview.com/Sales/SalesSearch?countyId=8'),response(detail.replace('Cape May','Monmouth').replace('Property Address:','Address:'),'https://salesweb.civilview.com/Sales/SaleDetails?PropertyId=1')]
        with patch('requests.Session',return_value=session):sources,gaps=S.county_sources('Monmouth','https://salesweb.civilview.com/Sales/SalesSearch?countyId=8')
        self.assertIn('$300,000.00',sources[0]['text'])

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

    def test_failure_note_names_the_gate_but_never_free_text(self):
        # S533: the 10-27 run recorded only "ValueError".
        for error,note in [(ValueError('shore_header'),'ValueError: shore_header'),
                           (ValueError('county_pdf_amount_alignment'),'ValueError: county_pdf_amount_alignment'),
                           (ValueError('Expecting value: line 1 column 1 (char 0)'),'ValueError'),
                           (KeyError('price'),'KeyError'),(RuntimeError('Private Street 12 sold'),'RuntimeError')]:
            with self.subTest(note=note),patch.object(S,'run',side_effect=error),patch.object(S.sys,'argv',['shore_schedule.py','--live']), \
                 patch('job_status.record') as record,patch('vendor_mail_watch.telegram',return_value=True),patch('builtins.print'):
                self.assertEqual(S.main(),1)
                record.assert_called_once_with('shoredigest',False,note)

if __name__=='__main__':unittest.main()
