import copy
import tempfile
import unittest
from datetime import datetime,timezone
from unittest.mock import patch
import stock_schedule as S

QUOTE='Synthetic revenue for the period ended 2026-09-30 was 100 units.'
EV={'companies':[{'ticker':'EXAMPLE','recorded_at':datetime.now(timezone.utc).isoformat(),
    'documents':[{'id':1,'text':QUOTE,'url':'https://example.com/filing','published':'2026-10-01'}]}]}
DEC={'decisions':[{'ticker':'EXAMPLE','action':'HOLD','reason':'Await supported valuation.',
    'valuation':'No verified valuation available.','counterargument':'Business evidence may deteriorate.',
    'exit_trigger':'Review a verified adverse business event.','evidence':[{'id':1,'quote':QUOTE}]}],
    'explanation':'No trades while valuation evidence remains incomplete; the primary filing remains context.'}

class ScheduleChecks(unittest.TestCase):
    def test_failed_review_is_a_failed_process_even_when_preview_was_composed(self):
        import io,contextlib
        with patch.object(S.sys,'argv',['stock_schedule.py','--job','am']),patch.object(S,'run',return_value={'status':'preview','decision_review':'failed_paper_decision_review_failed'}),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(S.main(),1)

    def test_http_success_without_dated_original_content_cannot_be_adopted(self):
        trials=[{'status':'fetched','text':QUOTE,'published':None}]
        choice={'trial':0,'decision':'ADOPT','reason':'Synthetic useful original article for a research question.','quote':QUOTE}
        with self.assertRaisesRegex(ValueError,'adoption_requires_original_dated_content'):
            S.validate_adoptions({'decisions':[choice]},trials)

    def test_adopted_source_is_read_by_daily_followup(self):
        from pathlib import Path
        from types import SimpleNamespace
        trials=[{'status':'fetched','text':QUOTE,'published':'2026-10-14','url':'https://example.com/article',
                 'observed_at':'2026-10-14T12:00:00Z','sha256':'synthetic-hash'}]
        choices=[{'trial':0,'decision':'ADOPT','reason':'Synthetic original dated content answers a research question.','quote':QUOTE}]
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)
            with patch.object(S,'HOME',home):
                S.install_adoptions(trials,choices)
                with patch.object(S.research,'fetch',return_value=(b'<html>synthetic</html>','text/html')) as fetch,patch.object(S.research,'Page',return_value=SimpleNamespace(title='Synthetic article',text=QUOTE*20,published='2026-10-14')):
                    result=S.public_followup()
            self.assertEqual(len(result['sources']),1);fetch.assert_called_once_with('https://example.com/article',['example.com'])
            self.assertTrue((home/'research/sources.md').exists())

    def test_fixed_window_keeps_sold_ideas_and_never_rewrites_prior_review(self):
        import json
        from pathlib import Path
        def q(price,at):return {'price':price,'quote_at':at,'valuation_eligible':True}
        d={'decision_id':'synthetic-original','ticker':'EXAMPLE','action':'BUY',
           'decided_at':'2026-09-01T15:00:00Z','review_dates':['2026-09-22','2026-09-29'],
           'review_reference':{'company':q('100','2026-09-01T14:59:00Z'),'spy':q('200','2026-09-01T14:59:00Z')}}
        account={'decisions':[d],'quotes':{'SPY':q('220','2026-10-14T15:00:00Z')}}
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);reviews=home/'fixed-window-reviews';reviews.mkdir()
            first=reviews/(S.review_key(d,'2026-09-22')+'.json');first.write_text('{"original":"unchanged"}')
            with patch.object(S.paper,'get_quote',return_value=q('105','2026-10-14T15:00:00Z')) as fetch,patch.object(S.paper,'validate_mark',side_effect=lambda x:x):
                rows,total=S.fixed_observations(account,'2026-10-14',home)
            self.assertEqual(total,1);self.assertEqual(rows[0]['scheduled_review'],'2026-09-29')
            self.assertTrue(rows[0]['observed_late']);self.assertEqual(rows[0]['excess_percentage_points'],'-5.00')
            self.assertEqual(first.read_text(),'{"original":"unchanged"}');fetch.assert_called_once()

    def test_missing_original_benchmark_is_not_invented(self):
        from pathlib import Path
        d={'ticker':'EXAMPLE','decided_at':'2026-09-01T15:00:00Z','review_dates':['2026-09-22']}
        with tempfile.TemporaryDirectory() as td,patch.object(S.paper,'validate_mark',side_effect=lambda x:x):
            rows,_=S.fixed_observations({'decisions':[d],'quotes':{'EXAMPLE':{},'SPY':{}}},'2026-10-14',Path(td))
        self.assertFalse(rows[0]['comparison_available']);self.assertIsNone(rows[0]['original_reference'])

    def test_primary_evidence_and_stable_identity(self):
        a,_=S.qualify_decisions(DEC,EV,{},'2026-10-14','am')
        b,_=S.qualify_decisions(DEC,EV,{},'2026-10-14','am')
        self.assertEqual(a,b);self.assertEqual(a['decisions'][0]['evidence'][0]['url'],'https://example.com/filing')

    def test_morning_never_turns_into_late_buy(self):
        d=copy.deepcopy(DEC);d['decisions'][0]['action']='BUY'
        with self.assertRaisesRegex(ValueError,'morning_plans_only'):S.qualify_decisions(d,EV,{},'2026-10-14','am')

    def test_unknown_company_refused(self):
        d=copy.deepcopy(DEC);d['decisions'][0]['ticker']='OTHER'
        with self.assertRaises(ValueError):S.qualify_decisions(d,EV,{},'2026-10-14','pm')

    def test_stale_research_refused(self):
        ev=copy.deepcopy(EV);ev['companies'][0]['recorded_at']='2020-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError,'research_not_current'):S.qualify_decisions(DEC,ev,{},'2026-10-14','pm')

    def test_invented_quote_refused(self):
        d=copy.deepcopy(DEC);d['decisions'][0]['evidence'][0]['quote']='Invented revenue was 200 units and growing very fast.'
        with self.assertRaisesRegex(ValueError,'decision_quote_or_date_unverified'):S.qualify_decisions(d,EV,{},'2026-10-14','pm')

    def test_review_dates_skip_holiday_and_weekend(self):
        self.assertEqual(S.review_dates('2026-11-05')[0],'2026-11-27')
        self.assertEqual(S.review_dates('2026-10-17')[0],'2026-11-09')

    def test_sent_slot_short_circuits_before_research_and_trades(self):
        import json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);(home/'delivery').mkdir();(home/'delivery/2026-10-14-am.json').write_text(json.dumps({'status':'sent'}))
            with patch.object(S,'HOME',home),patch.object(S,'snapshot') as snap,patch.object(S,'refresh_research') as research:
                result=S.daily('am',True,'2026-10-14',home)
            self.assertEqual(result['status'],'already_sent');snap.assert_not_called();research.assert_not_called()

    def test_uncertain_slot_never_resends(self):
        import json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);(home/'delivery').mkdir();(home/'delivery/2026-10-14-pm.json').write_text(json.dumps({'status':'sending_outcome_unknown_until_confirmed'}))
            with patch.object(S,'HOME',home),patch.object(S,'snapshot') as snap:
                with self.assertRaisesRegex(ValueError,'prior_delivery_requires_reconciliation'):S.daily('pm',True,'2026-10-14',home)
            snap.assert_not_called()

    def test_scorecard_preview_has_no_mail_trade_or_live_review_write(self):
        from pathlib import Path
        from contextlib import nullcontext
        account={'decisions':[],'report':'Synthetic canonical account','report_id':'fixture'}
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)
            with patch.object(S,'ROOT',folder),patch.object(S,'HOME',folder), \
                    patch.object(S,'snapshot',return_value=account), \
                    patch.object(S.media,'lease',return_value=nullcontext()),patch.object(S,'Ledger'), \
                    patch.object(S.paper,'tax_export',return_value={'dispositions':[]}), \
                    patch.object(S,'policy',return_value='Synthetic paper policy only.'), \
                    patch.object(S,'checked_prose',return_value='No mature decisions; synthetic account only.'), \
                    patch.object(S.sender,'send_verified_once') as send,patch.object(S.paper,'apply') as apply:
                result=S.scorecard(False,'2026-10-14',folder)
            self.assertEqual(result['status'],'preview');send.assert_not_called();apply.assert_not_called()
            self.assertTrue((folder/'preview.md').exists())
            self.assertFalse((folder/'fixed-window-reviews').exists())
            self.assertFalse((folder/'digests').exists())

if __name__=='__main__':unittest.main()
