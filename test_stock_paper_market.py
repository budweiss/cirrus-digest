#!/usr/bin/env python3
"""Deterministic quote/calendar gates; no network, real money, or persistent state."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from unittest.mock import patch

import stock_paper_market as market


AT = datetime(2026, 10, 8, 17, 50, 0, tzinfo=timezone.utc)


def packet(at=AT, ticker='MSFT', **changes):
    session = market.market_session(at)
    # A closure test still gets internally coherent metadata on the same date;
    # the independently sourced calendar must reject it.
    local = at.astimezone(market.NY)
    opened = session['open'] or local.replace(hour=9, minute=30, second=0).isoformat()
    closed = session['close'] or local.replace(hour=16, minute=0, second=0).isoformat()
    meta = {'symbol': ticker, 'longName': 'Microsoft Corporation', 'currency': 'USD',
            'regularMarketPrice': 123.4567, 'regularMarketTime': int(at.timestamp()),
            'exchangeName': 'NMS', 'exchangeTimezoneName': 'America/New_York',
            'instrumentType': 'EQUITY', 'currentTradingPeriod': {'regular': {
                'start': int(market._at(opened).timestamp()),
                'end': int(market._at(closed).timestamp())}}}
    meta.update(changes)
    return {'chart': {'error': None, 'result': [{'meta': meta}]}}


def quote(at=AT, ticker='MSFT', payload=None, headers=None, **changes):
    payload = payload if payload is not None else packet(at, ticker, **changes)
    raw = json.dumps(payload).encode()
    return market.get_quote(ticker, now=at, fetcher=lambda url: (raw, headers or {}))


class QuoteTests(unittest.TestCase):
    def test_fresh_last_trade_has_exact_decimal_and_honest_delay(self):
        q = quote()
        self.assertTrue(q['executable'], q['reasons'])
        self.assertEqual(q['price'], '123.4567')
        self.assertEqual(q['delay'], 'timestamp_fresh_delay_unverified')
        self.assertIsNone(q['delay_seconds'])
        self.assertIsNone(q['ask'])
        self.assertIsNone(q['bid'])
        self.assertEqual(q['quote_at'], '2026-10-08T17:50:00Z')
        self.assertEqual(q['market_state'], 'REGULAR')

    def test_freshness_boundary_and_future_limit(self):
        for offset, allowed in ((-120, True), (-121, False), (5, True), (6, False)):
            with self.subTest(offset=offset):
                q = quote(regularMarketTime=int((AT + timedelta(seconds=offset)).timestamp()))
                self.assertEqual(q['executable'], allowed, q['reasons'])
        q = quote()
        later = market.validate_quote(q, now=AT + timedelta(seconds=121))
        self.assertFalse(later['executable'])
        self.assertIn('quote_at_stale', later['reasons'])
        self.assertIn('fetched_at_stale', later['reasons'])

    def test_validation_default_uses_wall_clock(self):
        q = quote()
        real_at = market._at
        def clock(value=None):
            return real_at(AT + timedelta(seconds=200) if value is None else value)
        with patch.object(market, '_at', side_effect=clock):
            checked = market.validate_quote(q)
        self.assertFalse(checked['executable'])
        self.assertIn('quote_at_stale', checked['reasons'])
        self.assertEqual(checked['validated_at'], '2026-10-08T17:53:20Z')

    def test_identity_currency_and_instrument_mismatch(self):
        for change, code in (({'symbol': 'OTHER'}, 'provider_ticker_mismatch'),
                             ({'currency': 'CAD'}, 'currency_not_usd'),
                             ({'longName': '', 'shortName': ''}, 'company_identity_missing'),
                             ({'exchangeName': 'LSE'}, 'exchange_unsupported'),
                             ({'exchangeTimezoneName': 'UTC'}, 'exchange_timezone_invalid'),
                             ({'instrumentType': 'OPTION'}, 'instrument_unsupported')):
            with self.subTest(change=change):
                q = quote(**change)
                self.assertFalse(q['executable'])
                self.assertIn(code, q['reasons'])

    def test_price_and_timestamp_bad_values_never_pass(self):
        for price in (None, True, 0, -1, 'nan', 'Infinity', 'bad', '1e99999999', '1e-99999999'):
            with self.subTest(price=price):
                self.assertFalse(quote(regularMarketPrice=price)['executable'])
        for stamp in (None, True, '1791481800', float('nan'), 1791481800.1):
            with self.subTest(stamp=stamp):
                self.assertFalse(quote(regularMarketTime=stamp)['executable'])

    def test_explicit_delayed_zero_and_invalid_delay(self):
        delayed = quote(exchangeDataDelayedBy=15)
        self.assertEqual(delayed['delay'], 'delayed')
        self.assertFalse(delayed['executable'])
        self.assertEqual(delayed['delay_seconds'], 900)
        current = quote(exchangeDataDelayedBy=0)
        self.assertTrue(current['executable'], current['reasons'])
        self.assertEqual(current['delay'], 'provider_reported_realtime')
        for value in (True, -1, '0'):
            self.assertFalse(quote(exchangeDataDelayedBy=value)['executable'])

    def test_provider_cannot_open_calendar_or_change_session(self):
        self.assertFalse(quote(marketState='CLOSED')['executable'])
        p = packet()
        p['chart']['result'][0]['meta']['currentTradingPeriod']['regular']['end'] += 3600
        q = quote(payload=p)
        self.assertFalse(q['executable'])
        self.assertIn('provider_session_mismatch', q['reasons'])
        holiday = datetime(2026, 11, 26, 17, 50, tzinfo=timezone.utc)
        q = quote(at=holiday, marketState='REGULAR')
        self.assertFalse(q['executable'])
        self.assertIn('exchange_holiday', q['reasons'])

    def test_quote_must_belong_to_current_regular_session(self):
        opening = datetime(2026, 10, 8, 13, 30, 0, tzinfo=timezone.utc)
        q = quote(at=opening, regularMarketTime=int(opening.timestamp()) - 1)
        self.assertFalse(q['executable'])
        self.assertIn('quote_outside_current_session', q['reasons'])
        q = quote(at=opening)
        self.assertTrue(q['executable'], q['reasons'])

    def test_timezone_offsets_are_required(self):
        q = quote()
        for key in ('quote_at', 'fetched_at'):
            bad = dict(q, **{key: '2026-10-08T17:50:00'})
            checked = market.validate_quote(bad, now=AT)
            self.assertFalse(checked['executable'])
            self.assertIn(key + '_invalid', checked['reasons'])
        with self.assertRaisesRegex(ValueError, 'timezone_aware'):
            market.market_session(datetime(2026, 10, 8, 13, 50))

    def test_symbol_input_is_bounded_before_fetch(self):
        for ticker in ('', '^GSPC', 'BRK/B', 'AAPL?token=secret', 'x' * 100, None):
            fetcher = lambda url: self.fail('invalid symbol made a network request')
            self.assertFalse(market.get_quote(ticker, now=AT, fetcher=fetcher)['executable'])
        self.assertEqual(quote(ticker='SPY', exchangeName='PCX', instrumentType='ETF')['ticker'], 'SPY')

    def test_empty_malformed_ambiguous_and_error_responses_are_held(self):
        values = [b'', b'not-json', b'[]', b'{}',
                  b'{"chart":{"result":[],"error":null}}',
                  b'{"chart":{"result":null,"error":{"description":"private-body"}}}',
                  json.dumps({'chart': {'result': [{}, {}]}}).encode(),
                  json.dumps({'chart': {'result': [{'meta': {}}]}}).encode()]
        for raw in values:
            with self.subTest(raw=raw[:25]):
                q = market.get_quote('MSFT', now=AT, fetcher=lambda url: raw)
                self.assertFalse(q['executable'])
                self.assertNotIn('private-body', json.dumps(q))
        q = market.get_quote('MSFT', now=AT, fetcher=lambda url: b'x' * (market.MAX_BYTES + 1))
        self.assertFalse(q['executable'])

    def test_provider_failures_return_safe_codes_without_error_body(self):
        def denied(url):
            raise urllib.error.HTTPError('https://example.com/?token=private', 429,
                                         'credential=private', {}, None)
        q = market.get_quote('MSFT', now=AT, fetcher=denied)
        self.assertIn('provider_http_429', q['reasons'])
        self.assertNotIn('private', json.dumps(q))
        def network(url):
            raise urllib.error.URLError('token=private')
        self.assertFalse(market.get_quote('MSFT', now=AT, fetcher=network)['executable'])

    def test_http_timestamp_preserved_and_stale_response_held(self):
        headers = {'date': 'Thu, 08 Oct 2026 17:50:00 GMT', 'content-type': 'application/json'}
        q = quote(headers=headers)
        self.assertTrue(q['executable'], q['reasons'])
        self.assertEqual(q['http_date'], headers['date'])
        for day in ('Wed, 07 Oct 2026 17:50:00 GMT', 'Thu, 08 Oct 2026 17:50:06 GMT', 'bad'):
            q = quote(headers={'date': day})
            self.assertFalse(q['executable'])
            self.assertIn('http_response_time_invalid', q['reasons'])
        self.assertFalse(quote(headers={'content-type': 'text/html'})['executable'])

    def test_raw_archive_is_exact_private_content_addressed_and_idempotent(self):
        raw = json.dumps(packet()).encode()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'quotes'
            q = market.get_quote('MSFT', now=AT, fetcher=lambda url: raw, archive_dir=directory)
            self.assertTrue(q['executable'], q['reasons'])
            path = Path(q['raw_archive'])
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(path.stem, hashlib.sha256(raw).hexdigest())
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
            again = market.get_quote('MSFT', now=AT, fetcher=lambda url: raw, archive_dir=directory)
            self.assertEqual(again['raw_archive'], q['raw_archive'])
            self.assertEqual(len(list(directory.iterdir())), 1)
            path.write_text('tampered')
            held = market.get_quote('MSFT', now=AT, fetcher=lambda url: raw, archive_dir=directory)
            self.assertFalse(held['executable'])

    def test_revalidation_refuses_changed_identity_and_does_not_mutate_input(self):
        q = quote()
        old = copy.deepcopy(q)
        checked = market.validate_quote(q, ticker='SPY', side='BUY', now=AT)
        self.assertFalse(checked['executable'])
        self.assertIn('ticker_mismatch', checked['reasons'])
        self.assertEqual(q, old)
        self.assertFalse(market.validate_quote(q, side='HOLD', now=AT)['executable'])

    def test_bid_ask_need_their_own_fresh_timestamps(self):
        q = quote(bid=123.45, ask=123.46, bidTime=int(AT.timestamp()), askTime=int(AT.timestamp()))
        self.assertTrue(q['executable'], q['reasons'])
        self.assertEqual(q['bid'], '123.45')
        self.assertEqual(q['ask'], '123.46')
        self.assertFalse(quote(bid=123.50, ask=123.46, bidTime=int(AT.timestamp()),
                               askTime=int(AT.timestamp()))['executable'])
        self.assertFalse(quote(bid=123.45, ask=123.46, bidTime=int(AT.timestamp()) - 121,
                               askTime=int(AT.timestamp()))['executable'])
        q = quote(bid=123.45, ask=123.46)
        self.assertIsNone(q['bid'])
        self.assertIsNone(q['ask'])
        self.assertTrue(q['executable'], q['reasons'])
        q['bid'], q['ask'] = '123.45', '123.46'
        self.assertFalse(market.validate_quote(q, now=AT)['executable'])

    def test_corporate_actions_preserved_for_ledger_review(self):
        p = packet()
        p['chart']['result'][0]['events'] = {
            'dividends': {'1': {'date': int(AT.timestamp()), 'amount': 0.25}},
            'splits': {'2': {'date': int(AT.timestamp()), 'numerator': 2, 'denominator': 1}}}
        q = quote(payload=p)
        self.assertTrue(q['executable'], q['reasons'])
        self.assertEqual(q['corporate_actions'], [
            {'kind': 'dividend', 'at': '2026-10-08T17:50:00Z', 'amount': '0.25'},
            {'kind': 'split', 'at': '2026-10-08T17:50:00Z', 'numerator': '2', 'denominator': '1'}])
        p['chart']['result'][0]['events']['splits']['2']['denominator'] = 0
        self.assertFalse(quote(payload=p)['executable'])


class CalendarTests(unittest.TestCase):
    def test_regular_boundaries_are_half_open_and_ny_based(self):
        for value, state in (('2026-10-08T13:29:59Z', 'PRE'),
                             ('2026-10-08T13:30:00Z', 'REGULAR'),
                             ('2026-10-08T19:59:59Z', 'REGULAR'),
                             ('2026-10-08T20:00:00Z', 'POST')):
            self.assertEqual(market.market_session(value)['state'], state)
        self.assertEqual(market.market_session('2026-10-09T01:00:00Z')['date'], '2026-10-08')

    def test_dst_changes_utc_open_without_moving_ny_open(self):
        for value, opened, closed in (
                ('2026-03-06T16:00:00Z', '2026-03-06T14:30:00Z', '2026-03-06T21:00:00Z'),
                ('2026-03-09T16:00:00Z', '2026-03-09T13:30:00Z', '2026-03-09T20:00:00Z'),
                ('2026-10-30T16:00:00Z', '2026-10-30T13:30:00Z', '2026-10-30T20:00:00Z'),
                ('2026-11-02T16:00:00Z', '2026-11-02T14:30:00Z', '2026-11-02T21:00:00Z')):
            s = market.market_session(value)
            self.assertEqual((s['open'], s['close']), (opened, closed))

    def test_weekend_and_every_published_holiday_closed(self):
        for day in ('2026-10-10', '2026-10-11') + tuple(sorted(set().union(*market.HOLIDAYS.values()))):
            s = market.market_session(day + 'T16:00:00Z')
            self.assertEqual(s['state'], 'CLOSED', day)
            self.assertIsNone(s['open'])
        self.assertEqual(len(market.HOLIDAYS[2026]), 10)
        self.assertEqual(len(market.HOLIDAYS[2027]), 10)
        # Banks can close while the NYSE is open; do not conflate calendars.
        self.assertEqual(market.market_session('2026-10-12T16:00:00Z')['state'], 'REGULAR')
        self.assertEqual(market.market_session('2026-11-11T16:00:00Z')['state'], 'REGULAR')

    def test_early_closes_and_july2_not_inferred_from_bank_calendar(self):
        for day in ('2026-11-27', '2026-12-24', '2027-11-26'):
            before = market.market_session(day + 'T17:59:59Z')
            after = market.market_session(day + 'T18:00:00Z')
            self.assertTrue(before['early_close'])
            self.assertEqual(before['state'], 'REGULAR')
            self.assertEqual(after['state'], 'POST')
        july = market.market_session('2026-07-02T18:00:00Z')
        self.assertEqual(july['state'], 'REGULAR')
        self.assertFalse(july['early_close'])
        self.assertEqual(market.market_session('2027-12-31T16:00:00Z')['state'], 'REGULAR')

    def test_unverified_years_fail_closed(self):
        for year in (2025, 2028):
            self.assertEqual(market.market_session(str(year) + '-06-01T16:00:00Z')['state'],
                             'CALENDAR_UNVERIFIED')
            with self.assertRaisesRegex(ValueError, 'calendar_year_unverified'):
                market.is_trading_day(date(year, 6, 1))
        with self.assertRaisesRegex(ValueError, 'calendar_year_unverified'):
            market.next_trading_day(date(2027, 12, 31))

    def test_next_exchange_day_skips_weekends_holidays_and_crosses_year(self):
        for day, following in (('2026-07-02', '2026-07-06'),
                               ('2026-11-25', '2026-11-27'),
                               ('2026-11-27', '2026-11-30'),
                               ('2026-12-31', '2027-01-04'),
                               ('2027-06-17', '2027-06-21')):
            self.assertEqual(market.next_trading_day(day), date.fromisoformat(following))


class ValuationTests(unittest.TestCase):
    def mark(self, fetched, quoted, **changes):
        at = market._at(fetched)
        q = quote(at=at, regularMarketTime=int(market._at(quoted).timestamp()), **changes)
        return market.validate_mark(q, now=at)

    def test_current_regular_mark_keeps_fresh_trade_gate(self):
        q = market.validate_mark(quote(), now=AT)
        self.assertTrue(q['valuation_eligible'], q['valuation_reasons'])
        self.assertEqual(q['valuation_label'], 'current_regular_trade')
        q = market.validate_mark(quote(), now=AT + timedelta(seconds=121))
        self.assertFalse(q['valuation_eligible'])

    def test_friday_and_monday_morning_use_latest_completed_session(self):
        for fetched, closed in (('2026-10-09T11:45:00Z', '2026-10-08T20:00:00Z'),
                                 ('2026-10-12T11:45:00Z', '2026-10-09T20:00:00Z'),
                                 ('2026-09-08T11:45:00Z', '2026-09-04T20:00:00Z')):
            q = self.mark(fetched, closed, marketState='PRE')
            self.assertTrue(q['valuation_eligible'], q['valuation_reasons'])
            self.assertFalse(q['executable'])
            self.assertEqual(q['valuation_label'], 'previous_regular_close')
            self.assertEqual(q['quote_at'], closed)
            self.assertEqual(q['valuation_session_date'], closed[:10])

    def test_overnight_weekend_holiday_and_early_close_marks(self):
        for fetched, closed in (('2026-10-09T01:30:00Z', '2026-10-08T20:00:00Z'),
                                 ('2026-10-10T16:00:00Z', '2026-10-09T20:00:00Z'),
                                 ('2026-11-26T16:00:00Z', '2026-11-25T21:00:00Z'),
                                 ('2026-12-25T16:00:00Z', '2026-12-24T18:00:00Z')):
            q = self.mark(fetched, closed)
            self.assertTrue(q['valuation_eligible'], q['valuation_reasons'])
            self.assertFalse(q['executable'])

    def test_last_trade_label_and_final_hour_bound(self):
        q = self.mark('2026-10-09T11:45:00Z', '2026-10-08T19:00:00Z')
        self.assertTrue(q['valuation_eligible'], q['valuation_reasons'])
        self.assertEqual(q['valuation_label'], 'last_regular_trade')
        for quoted in ('2026-10-08T18:59:59Z', '2026-10-08T20:00:01Z', '2026-10-07T20:00:00Z'):
            q = self.mark('2026-10-09T11:45:00Z', quoted)
            self.assertFalse(q['valuation_eligible'], quoted)
            self.assertIsNone(q['valuation_label'])

    def test_marks_retain_identity_delay_fetch_and_future_guards(self):
        for change in ({'currency': 'CAD'}, {'symbol': 'OTHER'},
                       {'exchangeDataDelayedBy': 15}, {'marketState': 'HALTED'}):
            q = self.mark('2026-10-09T11:45:00Z', '2026-10-08T20:00:00Z', **change)
            self.assertFalse(q['valuation_eligible'])
        q = self.mark('2026-10-09T11:45:00Z', '2026-10-09T11:45:06Z')
        self.assertFalse(q['valuation_eligible'])
        q = self.mark('2026-10-09T11:45:00Z', '2026-10-08T20:00:00Z')
        q = market.validate_mark(q, now='2026-10-09T11:47:01Z')
        self.assertFalse(q['valuation_eligible'])
        self.assertIn('fetched_at_stale', q['valuation_reasons'])


if __name__ == '__main__':
    unittest.main()
