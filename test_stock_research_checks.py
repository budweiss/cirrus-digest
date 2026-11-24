import copy
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import stock_research_checks as C
import stock_schedule as S
import personal_schedule as P
from test_stock_schedule import DEC, EV, QUOTE

CHECKS = {k: 'Source evidence is missing; no finding can be established.' for k in C.HEADINGS}


class ResearchChecks(unittest.TestCase):
    def test_missing_check_and_unbounded_text_cannot_pass(self):
        for key in C.HEADINGS:
            value = dict(CHECKS); del value[key]
            with self.assertRaisesRegex(ValueError, 'required'): C.validate(value)
        value = dict(CHECKS, earnings='x' * 1001)
        with self.assertRaisesRegex(ValueError, 'length'): C.validate(value)
        value = {k: 'word ' * 80 for k in C.HEADINGS}
        with self.assertRaisesRegex(ValueError, 'word_budget'): C.validate(value)

    def test_history_preserves_first_expectation_and_rejects_hindsight(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            path = C.remember(home, '2026-11-20-am', CHECKS, '2026-11-20T12:00:00Z')
            original = path.read_bytes()
            C.remember(home, '2026-11-20-am', CHECKS, '2026-11-21T12:00:00Z')
            self.assertEqual(path.read_bytes(), original)
            with self.assertRaisesRegex(ValueError, 'conflict'):
                C.remember(home, '2026-11-20-am', dict(CHECKS, earnings='Rewritten after the event; must never replace the original.'), '2026-11-21T12:00:00Z')
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(C.history(home, '2026-11-20T11:00:00Z')['prior_reviews'])
            self.assertEqual(len(C.history(home, '2026-11-20T13:00:00Z')['prior_reviews']), 1)
            self.assertFalse(C.history(home / 'other-account', '2026-11-21T12:00:00Z')['prior_reviews'])
            row = json.loads(path.read_text()); row['checks']['earnings'] = 'Unverified edit must be excluded from later research.'
            path.write_text(json.dumps(row))
            self.assertEqual(C.history(home, '2026-11-21T12:00:00Z')['invalid_or_future_excluded'], 1)
            row['recorded_at'] = None; row['sha256'] = C.record_hash(row)
            path.write_text(json.dumps(row))
            self.assertEqual(C.history(home, '2026-11-21T12:00:00Z')['invalid_or_future_excluded'], 1)

    def test_original_reason_comes_from_executed_buy_not_unfilled_idea(self):
        account = {'decisions': [{'decision_id': 'unfilled', 'reason': 'Ignore unfilled plan.'},
                                 {'decision_id': 'buy', 'reason': 'Original business thesis.'}],
                   'trades': [{'ticker': 'EXAMPLE', 'side': 'BUY', 'decision_id': 'buy', 'executed_at': '2026-11-01T15:00:00Z'}]}
        self.assertEqual(C.original_buys(account, {'EXAMPLE'})['EXAMPLE']['reason'], 'Original business thesis.')
        self.assertFalse(C.original_buys(dict(account, trades=[]), {'EXAMPLE'}))

    def test_paper_model_and_independent_reviewer_receive_checks_and_history(self):
        evidence = copy.deepcopy(EV)
        evidence['companies'][0]['draft'] = {'paragraphs': [{'evidence': [{'id': 1, 'quote': QUOTE}]}]}
        prior = {'prior_reviews': [{'recorded_at': '2026-11-19T12:00:00Z', 'checks': CHECKS}]}
        seen = []
        def caller(system, text, task):
            seen.append((system, json.loads(text)))
            return json.dumps(DEC)
        with tempfile.TemporaryDirectory() as td, patch.object(S, 'ROOT', Path(td)), \
                patch.object(S, 'policy', return_value='Synthetic policy'), \
                patch.object(S.media, 'lease', return_value=nullcontext()), \
                patch.object(S.media, 'token_count', return_value=100), \
                patch.object(S.research, 'structured_review', return_value='{"passed":true,"issues":[]}') as review:
            request, _ = S.choose(evidence, {}, {}, '2026-11-20', 'am', caller=caller, prior=prior)
        self.assertEqual(request['research_checks'], DEC['research_checks'])
        self.assertEqual(seen[0][1]['research_history'], prior)
        self.assertIn('recorded BEFORE', seen[0][0])
        decision, checks = review.call_args_list
        self.assertNotIn('research_checks', decision.args[0])
        self.assertNotIn('research_checks', json.loads(decision.args[1])['proposed'])
        self.assertIn('hindsight', checks.args[0])
        self.assertEqual(json.loads(checks.args[1])['research_checks'], DEC['research_checks'])
        self.assertEqual(checks.args[3], C.CHECKS_ISSUE)
        rendered = C.render(request['research_checks'])
        for heading in C.HEADINGS.values(): self.assertIn(heading, rendered)

    def test_s679_research_check_figures_and_dates_must_be_in_the_supplied_context(self):
        context = json.dumps({'doc': 'Revenue was $1,234.56 million for the period ended 2026-09-30; results due November 5.'})
        supplied = dict(CHECKS, earnings='Revenue was 1234.56 million (about $1,235 million) for the period ended '
                                         '2026-09-30; results are due Nov. 5 and 3 metrics matter.')
        self.assertEqual(C.unsourced_figures(supplied, context), [])
        rescaled = dict(CHECKS, earnings='Revenue was $1.23456 billion, or about $1.235 billion, for the period.')
        self.assertEqual(C.unsourced_figures(rescaled, context), [])
        raw_dollars = json.dumps({'fact': {'Revenues': 15305000000}})
        self.assertEqual(C.unsourced_figures(dict(CHECKS, earnings='Revenue was $15.305 billion.'), raw_dollars), [])
        self.assertEqual(len(C.unsourced_figures(dict(CHECKS, earnings='Revenue was $153.05 billion.'), raw_dollars)), 1)
        invented = dict(CHECKS, earnings='Consensus EPS is $7.31 and results arrive 2026-12-04, guided to 14.6% growth.',
                        peers='The peer trades at 31.7 times earnings with operating margin 27.4%; results came December 9.')
        found = C.unsourced_figures(invented, context)
        self.assertEqual(sorted(v for _, v in found), sorted(['7.31', '2026-12-04', '14.6', '31.7', '27.4', 'December 9']))
        self.assertEqual({k for k, _ in found}, {'earnings', 'peers'})
        self.assertEqual(C.unsourced_figures(CHECKS, context), [])

    def _choose(self, outputs, verdicts):
        sent, audits = [], []
        def caller(system, text, task):
            sent.append(text)
            return json.dumps(outputs[min(len(sent), len(outputs)) - 1])
        with tempfile.TemporaryDirectory() as td, patch.object(S, 'ROOT', Path(td)), \
                patch.object(S, 'policy', return_value='Synthetic policy'), \
                patch.object(S.media, 'lease', return_value=nullcontext()), \
                patch.object(S.media, 'token_count', return_value=100), \
                patch.object(S.research, 'structured_review', side_effect=verdicts) as review:
            result = S.choose(copy.deepcopy(EV), {}, {}, '2026-11-24', 'am', caller=caller, audit=audits.append)
        return result, sent, audits, review

    def test_s679_unsourced_check_figure_never_reaches_the_checks_reviewer_and_is_named_for_the_retry(self):
        invented = copy.deepcopy(DEC); invented['research_checks']['earnings'] = 'Dated consensus is EPS $7.31 per share for the quarter.'
        ok = '{"passed":true,"issues":[]}'
        (request, _), sent, audits, review = self._choose([invented, DEC], [ok, ok, ok])
        self.assertEqual([a['stage'] for a in audits], ['review', 'checks_provenance', 'review', 'checks_review'])
        self.assertEqual(audits[1]['offenders'], [('earnings', '7.31')])
        self.assertIn('7.31', sent[1].split('Correct these research_checks issues:')[1])
        self.assertEqual(review.call_count, 3)
        self.assertEqual(request['research_checks'], DEC['research_checks'])

    def test_s679_rejected_checks_withhold_only_the_checklist_never_the_reviewed_decision(self):
        ok = '{"passed":true,"issues":[]}'
        bad = '{"passed":false,"issues":["research_checks.thesis: invented original buying reason"]}'
        (request, note), sent, audits, _ = self._choose([DEC], [ok, bad, ok, bad])
        self.assertNotIn('research_checks', request)
        self.assertEqual(len(request['decisions']), 1)
        self.assertEqual(audits[-1], {'stage': 'checks_withheld'})
        self.assertIn('invented original buying reason', sent[1])
        # A checklist retry that re-decides does not replace the first reviewed decision.
        (request, _), _, _, _ = self._choose([DEC, dict(DEC, decisions=[])], [ok, bad, ok, bad])
        self.assertEqual(len(request['decisions']), 1)
        # A decision the unchanged decision bar rejects is still held, checks or not.
        no = '{"passed":false,"issues":["Reason overstates coverage."]}'
        with self.assertRaisesRegex(ValueError, 'paper_decision_review_failed'):
            self._choose([DEC], [no, no])
        # A checks reviewer outage withholds the checklist, not the decision.
        (request, _), _, audits, _ = self._choose([DEC], [ok, RuntimeError('incomplete_or_wrong_review_model')] * 2)
        self.assertNotIn('research_checks', request)
        self.assertEqual(audits[1]['issues'], ['research_checks review unavailable: incomplete_or_wrong_review_model'])

    def test_personal_checks_cannot_invent_quantities_or_change_sell_rules(self):
        draft = {'opinion': 'Synthetic private opinion; no current conclusion is established.',
                 'rules': [{'rule': i, 'status': 'not_established', 'assessment': 'No source evidence establishes this test.'} for i in range(1, 8)],
                 'research_checks': dict(CHECKS, shared_risks='Invented exposure is 99%; this must be rejected.')}
        with self.assertRaisesRegex(ValueError, 'canonical_table') as exc:
            P.validate(draft, [])
        self.assertEqual(exc.exception.fields, {'research_checks'})
        fixed = copy.deepcopy(draft); fixed['research_checks'] = CHECKS
        fixed['rules'][0]['status'] = 'triggered'
        repaired = P.repair_fields(draft, fixed, {'research_checks'})
        self.assertEqual(repaired['rules'], draft['rules'])
        self.assertEqual(repaired['research_checks'], CHECKS)

    def test_personal_reviewer_checks_new_fields_and_preserves_other_sections(self):
        draft = {'opinion': 'Synthetic private opinion; no current conclusion is established.',
                 'rules': [{'rule': i, 'status': 'not_established', 'assessment': 'No source evidence establishes this test.'} for i in range(1, 8)],
                 'research_checks': dict(CHECKS)}
        revised = copy.deepcopy(draft)
        revised['research_checks']['thesis'] = 'Original owner buying reason is unavailable; request it before judging the thesis.'
        revised['opinion'] = 'This unsolicited rewrite must not survive the targeted repair.'
        rules = '\n'.join('%d. Synthetic source rule %d.' % (i, i) for i in range(1, 8))
        prior = {'prior_reviews': [{'recorded_at': '2026-11-19T12:00:00Z', 'checks': CHECKS}]}
        with tempfile.TemporaryDirectory() as td, patch.object(P.media, 'lease', return_value=nullcontext()), \
                patch.object(P, 'c1_draft', side_effect=[json.dumps(draft), json.dumps(revised)]) as model, \
                patch.object(P.research, 'structured_review', side_effect=[
                    '{"passed":false,"issues":["research_checks: State that the original owner buying reason is absent."]}',
                    '{"passed":true,"issues":[]}']) as reviewer:
            result = P.assess([], {}, [], [], 4, 'Historical background.', rules, Path(td), prior=prior)
        self.assertEqual(result['opinion'], draft['opinion'])
        self.assertEqual(result['research_checks'], revised['research_checks'])
        self.assertEqual(json.loads(model.call_args_list[0].args[1])['research_history'], prior)
        self.assertIn('research_checks', reviewer.call_args.kwargs['issue_pattern'])


if __name__ == '__main__': unittest.main()
