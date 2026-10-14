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
    def test_secondary_newsletters_exclude_other_mail_and_preserve_date_limits(self):
        from pathlib import Path
        from datetime import date
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)
            (folder/'20261014-sa.md').write_text('> From: `alerts@email.seekingalpha.com`\nSynthetic secondary opinion only.')
            (folder/'20261014-other.md').write_text('> From: `person@example.com`\nUnrelated private fixture.')
            (folder/'20261001-old.md').write_text('> From: `alerts@seekingalpha.com`\nOld fixture.')
            rows=S.secondary_newsletters(folder,date(2026,10,14))
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['archive_date'],'2026-10-14')
        self.assertIn('not an article publication date',rows[0]['notice'])

    def test_secondary_contribution_requires_both_offered_sources(self):
        public={'newsletter_excerpts':[{'id':'fixture','archive_date':'2026-10-14'}]}
        self.assertIn('contributed no',S.secondary_usage([],public,EV))
        with self.assertRaisesRegex(ValueError,'offered_primary_source'):
            S.secondary_usage([{'secondary_id':'fixture','primary_id':999}],public,EV)
        result=S.secondary_usage([{'secondary_id':'fixture','primary_id':1}],public,EV)
        self.assertIn('2026-10-01',result);self.assertIn('not a verified article date',result)
        listing=S.source_list(EV,public)
        self.assertIn('Primary sources',listing);self.assertIn('Secondary sources',listing)
        self.assertIn('full articles were not fetched',listing)

    def test_model_schema_limits_symbols_to_reviewed_companies(self):
        import json
        response={'model':S.media.MODEL,'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'decisions':[],'explanation':'Synthetic missing evidence means no action is justified.'})}}]}
        with patch.object(S.media,'token_count',return_value=100),patch.object(S.media,'request',return_value=response) as request,patch('llm_budget.record_call'):
            S.decision_complete('Synthetic policy','Synthetic evidence','am',EV['companies'])
        schema=request.call_args.args[1]['response_format']['json_schema']['schema']
        fields=schema['properties']['decisions']['items']['properties']
        self.assertEqual(fields['ticker']['enum'],['EXAMPLE']);self.assertEqual(fields['action']['enum'],['HOLD'])
        self.assertEqual(fields['evidence']['items']['properties']['id']['enum'],[1])

    def test_accepted_primary_quote_beyond_article_opening_is_preserved(self):
        import json
        from pathlib import Path
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as td:
            report=Path(td)/'report.md';report.write_text('Synthetic research')
            conn=MagicMock()
            conn.execute.side_effect=[SimpleCursor({'id':1,'started':'2026-10-14T12:00:00Z','finished':'2026-10-14T12:01:00Z','status':'partial','report':str(report)}),
                [{'ticker':'EXAMPLE','recorded_at':'2026-10-14T12:01:00Z','draft':json.dumps({'paragraphs':[{'evidence':[{'id':1,'quote':QUOTE}]}]})}],
                SimpleCursor({'data':json.dumps({'company':{},'documents':[{'id':1,'text':'x'*8000+QUOTE,'url':'https://example.com/filing','published':'2026-10-01'}]})})]
            with patch.object(S.research,'database',return_value=conn):out=S.packet()
            self.assertIn(QUOTE,out['companies'][0]['documents'][0]['text'])

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
                    patch.object(S,'checked_prose',return_value='No mature decisions; synthetic account only.') as narrative, \
                    patch.object(S.sender,'send_verified_once') as send,patch.object(S.paper,'apply') as apply:
                result=S.scorecard(False,'2026-10-14',folder)
            self.assertEqual(result['status'],'preview');send.assert_not_called();apply.assert_not_called()
            narrative.assert_not_called()
            self.assertIn('No recorded decision has reached', (folder/'preview.md').read_text())
            self.assertTrue((folder/'preview.md').exists())
            self.assertFalse((folder/'fixed-window-reviews').exists())
            self.assertFalse((folder/'digests').exists())

    def test_completed_scorecard_stays_out_of_shared_digest_ingestion(self):
        from pathlib import Path
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);home=root/'private/stock-paper';home.mkdir(parents=True)
            folder=home/'runs';folder.mkdir()
            account={'decisions':[],'report':'Synthetic private account','report_id':'fixture'}
            with patch.object(S,'ROOT',root),patch.object(S,'HOME',home),patch.object(S,'snapshot',return_value=account),patch.object(S.media,'lease',return_value=nullcontext()),patch.object(S,'Ledger'),patch.object(S.paper,'tax_export',return_value={'dispositions':[]}),patch.object(S,'policy',return_value='Synthetic policy'),patch.object(S,'checked_prose',return_value='Synthetic private scorecard'):
                S.scorecard(True,'2026-10-14',folder)
            self.assertTrue((home/'digests/stock-scorecard-latest.md').exists())
            self.assertFalse((root/'digests').exists())

class SimpleCursor:
    def __init__(self,row):self.row=row
    def fetchone(self):return self.row

if __name__=='__main__':unittest.main()
