import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import personal_schedule as S

class PersonalChecks(unittest.TestCase):
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
