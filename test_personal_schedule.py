import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import personal_schedule as S

class PersonalChecks(unittest.TestCase):
    def test_computed_rule_evidence_mixes_verified_quotes_and_export_values(self):
        rows=[{'ticker':'AAA','shares':'100','value_at_export':'10000','price_at_export':'100'},
              {'ticker':'BBB','shares':'50','value_at_export':'9000','price_at_export':'180'},
              {'ticker':'CCC','shares':'10','value_at_export':'','price_at_export':'50'},
              {'ticker':'DDD','shares':'','value_at_export':'','price_at_export':''}]
        marks={'AAA':{'valuation_eligible':True,'price':'210.50'},
               'BBB':{'valuation_eligible':False}}
        record,table_text=S.computed_rule_evidence(rows,marks)
        self.assertEqual(record['tickers']['AAA']['basis'],'verified_quote')
        # AAA uses the verified quote: 100 * 210.50 = 21050
        self.assertEqual(record['tickers']['AAA']['value_estimate'],'21050.00')
        self.assertEqual(record['tickers']['BBB']['basis'],'export_value')
        # CCC has no value cell but shares x export price = 500, so it still
        # counts (S418: partial rows previously dropped out of the denominator
        # and the weight column summed to ~50%).
        self.assertEqual(record['tickers']['CCC']['value_estimate'],'500')
        self.assertEqual(record['tickers']['CCC']['basis'],'export_value')
        # DDD has nothing at all and stays unknown.
        self.assertEqual(record['tickers']['DDD']['value_estimate'],None)
        self.assertEqual(record['tickers']['DDD']['basis'],'unknown')
        self.assertNotIn('DDD: weight_estimate_pct',table_text)
        # Denominator sanity: every known row is in the weight column and the
        # weights sum to ~100 percent.
        known=[r for r in record['tickers'].values() if r.get('weight_estimate_pct')]
        self.assertEqual(len(known),3)
        total=sum(float(r['weight_estimate_pct']) for r in known)
        self.assertAlmostEqual(total,100.0,delta=0.01)
        total=21050+9000
        self.assertEqual(record['tickers']['AAA']['weight_estimate_pct'],str((__import__('decimal').Decimal(21050)/total*100).quantize(__import__('decimal').Decimal('0.01'))))
        self.assertIn('denominator covers only exported holdings',record['method'])

    def test_private_draft_uses_only_c1_loopback_and_rejects_incomplete_output(self):
        import io
        response={'model':'gpt-oss:120b','done':True,'done_reason':'stop','message':{'content':'{"synthetic":true}'}}
        def reply(*args,**kwargs):return io.BytesIO(json.dumps(response).encode())
        with patch.object(S.media,'token_count',return_value=100),patch.object(S.urllib.request,'urlopen',side_effect=reply) as request,patch('llm_budget.record_call'):
            self.assertEqual(S.c1_draft('Synthetic policy','Synthetic private fixture'),'{"synthetic":true}')
            self.assertEqual(request.call_args.args[0].full_url,'http://127.0.0.1:11434/api/chat')
            response['done_reason']='length'
            with self.assertRaisesRegex(RuntimeError,'incomplete_or_wrong'):S.c1_draft('policy','fixture')
            response.update(done_reason='stop',model='other-model')
            with self.assertRaisesRegex(RuntimeError,'incomplete_or_wrong'):S.c1_draft('policy','fixture')

    def test_partial_paywall_text_is_not_treated_as_verified_news(self):
        from types import SimpleNamespace
        fetch=Mock(side_effect=[('Blocked excerpt '*40,True),('Verified public source '*40,False)])
        module=SimpleNamespace(search_web=lambda *a,**k:['https://example.com/blocked','https://example.com/allowed'],fetch_article_content=fetch)
        with patch.dict('sys.modules',{'cirrus_daily':module}):rows,gaps=S.public_news([{'ticker':'EXAMPLE','weight_pct':'100'}])
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['url'],'https://example.com/allowed')
        self.assertIsInstance(rows[0]['title'],str);self.assertEqual(fetch.call_args.kwargs['max_chars'],5000)

    def test_inconsistent_approval_preserves_findings_for_correction(self):
        from contextlib import nullcontext
        draft={'opinion':'Synthetic private opinion with limited evidence and no transactions.',
               'rules':[{'rule':i,'status':'not_established','assessment':'Synthetic test evidence is not available for this rule.'} for i in range(1,8)]}
        reviews=iter(['{"passed":true,"issues":["Missing the actual test for rule seven."]}','{"passed":true,"issues":[]}'])
        with tempfile.TemporaryDirectory() as td,patch.object(S.media,'lease',return_value=nullcontext()),patch.object(S,'c1_draft',return_value=json.dumps(draft)) as model,patch.object(S.research,'structured_review',side_effect=lambda *args:next(reviews)):
            result=S.assess([],{},[],[],4,'Synthetic seven rules.','Synthetic instructions.',Path(td))
            checks=json.loads((Path(td)/'checks.json').read_text())
        self.assertEqual(result,draft);self.assertEqual(model.call_count,2)
        self.assertFalse(checks[0]['passed']);self.assertIn('rule seven',model.call_args_list[1].args[1])

    def test_export_notice_does_not_depend_on_model_disclosure(self):
        self.assertIn('STALE',S.export_notice(4.1))
        self.assertIn('current positions need your confirmation',S.export_notice(4.1))
        self.assertNotIn('STALE',S.export_notice(1.0))

    def fixture(self,folder):
        raw=b'ticker,shares,avg_cost,price_at_export,value_at_export,weight_pct\nEXAMPLE,2,10,12,24,100\n'
        (folder/'holdings.csv').write_bytes(raw)
        (folder/'manifest.json').write_text(json.dumps({'sha256':hashlib.sha256(raw).hexdigest(),'source_mtime':time.time()-4*86400}))
        (folder/'sell-review-2026-10-09.md').write_text('Synthetic seven-rule instructions only.')
        (folder/'instructions.md').write_text('Synthetic private report; no brokerage action.')

    def test_stale_export_age_survives_copy(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);self.fixture(folder)
            rows,manifest,age,*_=S.inputs(folder)
            self.assertEqual(len(rows),1);self.assertGreater(age,3)

    def test_changed_csv_requires_matching_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);self.fixture(folder);(folder/'holdings.csv').write_text('changed')
            with self.assertRaisesRegex(ValueError,'holdings_hash_mismatch'):S.inputs(folder)

    def test_blank_numeric_cells_remain_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);self.fixture(folder)
            raw=(folder/'holdings.csv').read_text().replace('EXAMPLE,2,10,12,24,100','EXAMPLE,,,12,24,')
            (folder/'holdings.csv').write_text(raw)
            manifest=json.loads((folder/'manifest.json').read_text());manifest['sha256']=hashlib.sha256(raw.encode()).hexdigest()
            (folder/'manifest.json').write_text(json.dumps(manifest))
            rows,*_=S.inputs(folder)
            self.assertIsNone(rows[0]['shares']);self.assertIsNone(rows[0]['avg_cost']);self.assertIsNone(rows[0]['weight_pct'])
            self.assertEqual(rows[0]['value_at_export'],'24')

    def test_invalid_import_preserves_last_usable_inputs(self):
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);folder=home/'inputs';folder.mkdir();self.fixture(folder)
            before={p.name:p.read_bytes() for p in folder.iterdir()}
            raw='ticker,shares,avg_cost,price_at_export,value_at_export,weight_pct\nINVALID!,2,10,12,24,100\n'
            data={'files':{name:before[name].decode() for name in ('holdings.csv','sell-review-2026-10-09.md','instructions.md')},
                  'manifest':{'sha256':hashlib.sha256(raw.encode()).hexdigest(),'source_mtime':time.time()-86400}}
            data['files']['holdings.csv']=raw
            with patch.object(S.socket,'gethostname',return_value='cumulus1'),patch.object(S,'HOME',home),patch.object(S,'INPUTS',folder),patch.object(S.media,'lease',return_value=nullcontext()):
                with self.assertRaisesRegex(ValueError,'holding_symbol'):S.import_inputs(data)
            self.assertEqual(before,{p.name:p.read_bytes() for p in folder.iterdir()})

    def test_all_seven_rules_required(self):
        data={'opinion':'Synthetic private opinion with clearly limited evidence and no transactions.',
              'rules':[{'rule':i,'status':'not_established','assessment':'No synthetic trigger evidence established.'} for i in range(1,8)]}
        self.assertEqual(len(S.validate(data,[])['rules']),7)
        data['rules'].pop()
        with self.assertRaisesRegex(ValueError,'seven_sell_rules_required'):S.validate(data,[])

    def test_model_cannot_recalculate_financial_numbers_in_opinion(self):
        data={'opinion':'Synthetic dated export is 40% in one holding; this invented weight must be rejected.',
              'rules':[{'rule':i,'status':'not_established','assessment':'No synthetic evidence established.'} for i in range(1,8)]}
        with self.assertRaisesRegex(ValueError,'canonical_table'):S.validate(data,[])
        data['opinion']='Synthetic user rule defines a 40% threshold; no current outcome is established.'
        self.assertEqual(S.validate(data,[],'Synthetic rule threshold is 40%.'),data)

    def test_unoffered_source_refused(self):
        data={'opinion':'Synthetic opinion with invented source https://example.com/not-offered and no transactions.',
              'rules':[{'rule':i,'status':'not_established','assessment':'No synthetic evidence established.'} for i in range(1,8)]}
        with self.assertRaisesRegex(ValueError,'opinion_unoffered_source'):S.validate(data,[])

    def test_confirmed_delivery_skips_all_private_processing(self):
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td);receipts=folder/'delivery';receipts.mkdir()
            (receipts/(S.datetime.now(S.NY).date().isoformat()+'.json')).write_text(json.dumps({'status':'sent'}))
            with patch.object(S.socket,'gethostname',return_value='cumulus1'),patch.object(S,'HOME',folder),patch.object(S.sender,'RECEIPTS',receipts),patch.object(S.media,'lease',return_value=nullcontext()),patch.object(S,'inputs') as inputs,patch.object(S.sender,'send_live') as send:
                result=S.run(True)
            self.assertEqual(result['status'],'already_sent');inputs.assert_not_called();send.assert_not_called()

if __name__=='__main__':unittest.main()
