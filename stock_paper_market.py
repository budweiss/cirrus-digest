#!/usr/bin/env python3
"""Timestamped public observations for the independent paper portfolio.

This is a paper-only eligibility gate, never a broker/exchange execution promise.
Yahoo's unauthenticated chart endpoint supplies a last-trade observation, not a
guaranteed NBBO. Unknown feed delay stays explicitly unknown even when its trade
timestamp is fresh. Explicitly delayed observations are held. Callers simulate
fills with their documented adverse slippage and must revalidate at execution.

Calendar: https://www.nyse.com/trade/hours-calendars (checked 2026-10-08).
Only the published 2026/2027 cash-equity calendar is covered; other years close
the gate. New York zoneinfo applies DST rather than a hard-coded UTC offset.
The published calendar cannot anticipate unscheduled exchange closures/halts;
provider session metadata and freshness add checks, not a halt guarantee.
Provider semantics: https://help.yahoo.com/kb/finance/article-exchanges-data-delays-sln2310.html
Yahoo's market coverage table describes feed delays, but a chart response without
an explicit delay field does not itself prove a zero-delay feed. No credentials,
cookie acquisition, paid service, or brokerage connection is used here.
"""

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo


UTC = timezone.utc
NY = ZoneInfo('America/New_York')
MAX_QUOTE_AGE_SECONDS = 120
MAX_FUTURE_SECONDS = 5
MAX_CLOSE_OBSERVATION_AGE_SECONDS = 60 * 60
MAX_BYTES = 2_000_000
SOURCE = 'Yahoo public chart'
CALENDAR_SOURCE = 'https://www.nyse.com/trade/hours-calendars'
DELAY_SOURCE = 'https://help.yahoo.com/kb/finance/article-exchanges-data-delays-sln2310.html'
CALENDAR_CHECKED_AT = '2026-10-08'
HOLIDAYS = {
    2026: frozenset(('2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03',
                     '2026-05-25', '2026-06-19', '2026-07-03', '2026-09-07',
                     '2026-11-26', '2026-12-25')),
    2027: frozenset(('2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26',
                     '2027-05-31', '2027-06-18', '2027-07-05', '2027-09-06',
                     '2027-11-25', '2027-12-24')),
}
EARLY_CLOSES = frozenset(('2026-11-27', '2026-12-24', '2027-11-26'))
US_EXCHANGES = frozenset(('NMS', 'NGM', 'NCM', 'NYQ', 'PCX', 'ASE', 'BTS', 'BATS'))
ACCEPTED_DELAYS = frozenset(('provider_reported_realtime', 'timestamp_fresh_delay_unverified'))
_SYMBOL = re.compile(r'^[A-Z][A-Z0-9]{0,8}(?:-[A-Z0-9]{1,2})?$')


def _at(value=None):
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('timezone_aware_timestamp_required')
    return value.astimezone(UTC)


def _iso(value):
    return _at(value).isoformat().replace('+00:00', 'Z')


def _symbol(value):
    if not isinstance(value, str) or not _SYMBOL.fullmatch(value.strip().upper()):
        raise ValueError('ticker_invalid')
    return value.strip().upper()


def _price(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('price_invalid')
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('price_invalid') from None
    if (not number.is_finite() or number < Decimal('0.00000001')
            or number > Decimal('1000000000') or len(number.as_tuple().digits) > 32):
        raise ValueError('price_invalid')
    return format(number, 'f')


def _epoch(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError('provider_timestamp_invalid')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError('provider_timestamp_invalid')
        return datetime.fromtimestamp(int(number), UTC)
    except (ValueError, OverflowError, OSError):
        raise ValueError('provider_timestamp_invalid') from None


def _day(value):
    if isinstance(value, datetime):
        return _at(value).astimezone(NY).date()
    if isinstance(value, str):
        return date.fromisoformat(value)
    if not isinstance(value, date):
        raise ValueError('date_required')
    return value


def is_trading_day(value):
    """Return published cash-equity calendar status; refuse uncovered years."""
    day = _day(value)
    if day.year not in HOLIDAYS:
        raise ValueError('calendar_year_unverified')
    return day.weekday() < 5 and day.isoformat() not in HOLIDAYS[day.year]


def next_trading_day(value):
    """First exchange trading date strictly after value (not a bank calendar)."""
    day = _day(value)
    if day.year not in HOLIDAYS:
        raise ValueError('calendar_year_unverified')
    for _ in range(10):
        day += timedelta(days=1)
        if is_trading_day(day):
            return day
    raise ValueError('next_session_unverified')


def previous_trading_day(value):
    """Last published exchange date strictly before value."""
    day = _day(value)
    if day.year not in HOLIDAYS:
        raise ValueError('calendar_year_unverified')
    for _ in range(10):
        day -= timedelta(days=1)
        if is_trading_day(day):
            return day
    raise ValueError('previous_session_unverified')


def market_session(now=None):
    at = _at(now)
    local = at.astimezone(NY)
    day = local.date()
    answer = {'state': 'CLOSED', 'open': None, 'close': None,
              'date': day.isoformat(), 'early_close': False, 'reason': '',
              'calendar_source': CALENDAR_SOURCE, 'calendar_checked_at': CALENDAR_CHECKED_AT}
    if day.year not in HOLIDAYS:
        answer.update(state='CALENDAR_UNVERIFIED', reason='calendar_year_unverified')
        return answer
    if day.weekday() >= 5:
        answer['reason'] = 'weekend'
        return answer
    if day.isoformat() in HOLIDAYS[day.year]:
        answer['reason'] = 'exchange_holiday'
        return answer
    early = day.isoformat() in EARLY_CLOSES
    opened = datetime.combine(day, time(9, 30), NY)
    closed = datetime.combine(day, time(13 if early else 16), NY)
    state = 'PRE' if local < opened else 'REGULAR' if local < closed else 'POST'
    answer.update(state=state, open=_iso(opened), close=_iso(closed), early_close=early,
                  reason='' if state == 'REGULAR' else 'outside_regular_session')
    return answer


def quote_url(ticker):
    ticker = _symbol(ticker)
    return ('https://query1.finance.yahoo.com/v8/finance/chart/' + ticker
            + '?interval=1m&range=5d&includePrePost=false&events=div%2Csplits')


def _fetch(url):
    """One bounded public request. No redirects, proxies, cookies or auth."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ValueError('provider_redirect_refused')

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (StockPaper/1.0; private paper research)',
        'Accept': 'application/json', 'Cache-Control': 'no-cache'})
    with opener.open(req, timeout=15) as response:
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('provider_response_too_large')
        # Do not archive arbitrary headers; Set-Cookie can contain identifiers.
        return raw, {'date': response.headers.get('Date'),
                     'content-type': response.headers.get_content_type()}


def _archive(raw, directory):
    digest = hashlib.sha256(raw).hexdigest()
    if directory is None:
        return None, digest
    directory = Path(directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / (digest + '.json')
    try:
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError('quote_archive_integrity_failed')
    else:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
    return str(target.resolve()), digest


def _actions(result):
    answer = []
    events = result.get('events', {})
    if not isinstance(events, dict):
        raise ValueError('corporate_action_response_invalid')
    for field, kind in (('dividends', 'dividend'), ('splits', 'split')):
        rows = events.get(field, {})
        if not isinstance(rows, dict):
            raise ValueError('corporate_action_response_invalid')
        for row in rows.values():
            if not isinstance(row, dict):
                raise ValueError('corporate_action_response_invalid')
            event = {'kind': kind, 'at': _iso(_epoch(row.get('date')))}
            if kind == 'dividend':
                event['amount'] = _price(row.get('amount'))
            else:
                event.update(numerator=_price(row.get('numerator')),
                             denominator=_price(row.get('denominator')))
            answer.append(event)
    return sorted(answer, key=lambda row: (row['at'], row['kind']))


def validate_quote(quote, ticker=None, side=None, now=None):
    """Recompute paper eligibility using actual current time by default.

    ``now`` is injectable for deterministic tests; live callers omit it. The
    accepted unknown-delay label is intentionally not a realtime assertion.
    ``executable`` means eligible for this *paper* fill model only.
    """
    at = _at(now)
    out = dict(quote) if isinstance(quote, dict) else {}
    reasons = list(out.get('source_errors') or [])
    session = market_session(at)
    out.update(market_state=session['state'], session=session, validated_at=_iso(at))
    try:
        expected = _symbol(ticker if ticker is not None else out.get('ticker'))
        if out.get('ticker') != expected:
            reasons.append('ticker_mismatch')
    except ValueError:
        expected = None
        reasons.append('ticker_invalid')
    if not isinstance(out.get('name'), str) or not out['name'].strip():
        reasons.append('company_identity_missing')
    if out.get('currency') != 'USD':
        reasons.append('currency_not_usd')
    try:
        out['price'] = _price(out.get('price'))
    except ValueError:
        reasons.append('price_invalid')
    if out.get('source') != SOURCE or expected is None or out.get('url') != quote_url(expected):
        reasons.append('source_identity_invalid')
    if out.get('exchange') not in US_EXCHANGES:
        reasons.append('exchange_unsupported')
    if out.get('instrument_type') not in ('EQUITY', 'ETF'):
        reasons.append('instrument_unsupported')
    if out.get('exchange_timezone') != 'America/New_York':
        reasons.append('exchange_timezone_invalid')
    if out.get('delay') not in ACCEPTED_DELAYS:
        reasons.append('delayed_or_unverified_quote')
    if out.get('provider_market_state') not in (None, 'REGULAR'):
        reasons.append('provider_market_not_regular')
    if session['state'] != 'REGULAR':
        reasons.append(session['reason'] or 'market_not_regular')
    quoted = fetched = None
    for key in ('quote_at', 'fetched_at'):
        try:
            stamp = _at(out.get(key)) if out.get(key) is not None else None
            if stamp is None:
                raise ValueError('timestamp_missing')
            age = (at - stamp).total_seconds()
            if age > MAX_QUOTE_AGE_SECONDS:
                reasons.append(key + '_stale')
            if age < -MAX_FUTURE_SECONDS:
                reasons.append(key + '_future')
            if key == 'quote_at':
                quoted = stamp
                out['age_seconds'] = round(age, 3)
            else:
                fetched = stamp
            out[key] = _iso(stamp)
        except (ValueError, TypeError, OverflowError):
            reasons.append(key + '_invalid')
    if quoted and fetched and (quoted - fetched).total_seconds() > MAX_FUTURE_SECONDS:
        reasons.append('quote_after_fetch')
    if quoted and session['open'] and not (_at(session['open']) <= quoted < _at(session['close'])):
        reasons.append('quote_outside_current_session')
    for key, boundary in (('provider_session_open', 'open'), ('provider_session_close', 'close')):
        try:
            if out.get(key) is None or session[boundary] is None or _at(out[key]) != _at(session[boundary]):
                reasons.append('provider_session_mismatch')
        except (ValueError, TypeError):
            reasons.append('provider_session_invalid')
    if out.get('http_date'):
        try:
            stamp = parsedate_to_datetime(out['http_date'])
            delta = (at - _at(stamp)).total_seconds()
            if delta > MAX_QUOTE_AGE_SECONDS or delta < -MAX_FUTURE_SECONDS:
                reasons.append('http_response_time_invalid')
        except (ValueError, TypeError, OverflowError):
            reasons.append('http_response_time_invalid')
    if side is not None and side not in ('BUY', 'SELL'):
        reasons.append('side_invalid')
    if out.get('bid') is not None or out.get('ask') is not None:
        try:
            bid, ask = Decimal(_price(out.get('bid'))), Decimal(_price(out.get('ask')))
            if bid > ask:
                reasons.append('crossed_bid_ask')
            for key in ('bid_at', 'ask_at'):
                if not out.get(key):
                    raise ValueError('bid_ask_timestamp_missing')
                stamp = _at(out[key])
                age = (at - stamp).total_seconds()
                if not -MAX_FUTURE_SECONDS <= age <= MAX_QUOTE_AGE_SECONDS:
                    reasons.append('bid_ask_timestamp_invalid')
                if not session['open'] or not (_at(session['open']) <= stamp < _at(session['close'])):
                    reasons.append('bid_ask_outside_current_session')
        except (ValueError, TypeError, InvalidOperation):
            reasons.append('bid_ask_invalid')
    out['reasons'] = sorted(set(reasons))
    out['executable'] = not out['reasons']
    return out


def validate_mark(quote, now=None):
    """Value without opening the trading gate while the exchange is closed.

    Fetch evidence must still be <=120 seconds old. During regular hours only
    the usual fresh observation is eligible. At other times require a trade in
    the final hour of the latest completed regular session, through its exact
    close. This is an explicitly dated last-trade mark, not a current quote;
    only a timestamp equal to the close receives the close label. Uncovered
    calendar years and missing/old evidence produce no portfolio value.
    """
    at = _at(now)
    out = validate_quote(quote, now=at)
    out.update(valuation_eligible=False, valuation_label=None,
               valuation_session_date=None, valuation_reasons=list(out['reasons']))
    if out['market_state'] == 'REGULAR':
        out['valuation_eligible'] = out['executable']
        if out['valuation_eligible']:
            out.update(valuation_label='current_regular_trade',
                       valuation_session_date=out['session']['date'])
        return out
    if out['market_state'] == 'CALENDAR_UNVERIFIED':
        return out
    # These are execution-only restrictions. All identity, fresh-fetch,
    # explicit-delay, malformed-data, archive and future-time failures remain.
    execution_only = {'quote_at_stale', 'quote_outside_current_session',
                      'outside_regular_session', 'weekend', 'exchange_holiday',
                      'provider_session_mismatch'}
    reasons = [reason for reason in out['reasons'] if reason not in execution_only]
    if out.get('provider_market_state') in ('PRE', 'PREPRE', 'POST', 'POSTPOST', 'CLOSED'):
        reasons = [r for r in reasons if r != 'provider_market_not_regular']
    try:
        day = at.astimezone(NY).date()
        completed = day if out['market_state'] == 'POST' else previous_trading_day(day)
        session = market_session(datetime.combine(completed, time(12), NY))
        closed = _at(session['close'])
        quoted = _at(out['quote_at']) if out.get('quote_at') else None
        if (quoted is None or not closed - timedelta(seconds=MAX_CLOSE_OBSERVATION_AGE_SECONDS)
                <= quoted <= closed):
            reasons.append('latest_completed_session_quote_missing')
        if closed > at:
            reasons.append('valuation_session_not_complete')
        out['valuation_session_date'] = completed.isoformat()
        if not reasons:
            out['valuation_label'] = ('previous_regular_close' if quoted == closed
                                      else 'last_regular_trade')
    except (ValueError, TypeError, OverflowError):
        reasons.append('valuation_calendar_or_timestamp_unverified')
    out['valuation_reasons'] = sorted(set(reasons))
    out['valuation_eligible'] = not out['valuation_reasons']
    return out


def get_quote(ticker, now=None, fetcher=None, archive_dir=None):
    """Fetch once and return a JSON-safe held or eligible paper observation.

    fetcher(url) may supply bytes or (bytes, {'date': HTTP-Date, 'content-type':
    MIME}) in tests. Raw JSON is preserved unchanged when archive_dir is given.
    Provider error text/bodies and arbitrary response headers are never emitted.
    """
    started = _at(now)
    out = {'ticker': ticker, 'name': None, 'currency': None, 'price': None,
           'quote_at': None, 'fetched_at': _iso(started), 'source': SOURCE, 'url': None,
           'delay': 'unknown', 'delay_source': DELAY_SOURCE,
           'delay_seconds': None, 'market_state': None, 'executable': False,
           'reasons': [], 'source_errors': [], 'bid': None, 'ask': None,
           'bid_at': None, 'ask_at': None, 'raw_archive': None, 'raw_sha256': None,
           'http_date': None, 'exchange': None, 'exchange_timezone': None,
           'instrument_type': None, 'corporate_actions': [],
           'basis': 'Last-trade paper simulation; no guaranteed bid/ask, liquidity or real fill.'}
    try:
        ticker = _symbol(ticker)
        out.update(ticker=ticker, url=quote_url(ticker))
        response = (fetcher or _fetch)(out['url'])
        raw, headers = response if isinstance(response, tuple) else (response, {})
        fetched = _at(now)
        out['fetched_at'] = _iso(fetched)
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
            raise ValueError('provider_response_empty_or_large')
        if not isinstance(headers, dict):
            raise ValueError('provider_headers_invalid')
        mime = headers.get('content-type')
        if mime and mime.split(';', 1)[0].strip() != 'application/json':
            raise ValueError('provider_content_type_invalid')
        out['http_date'] = headers.get('date')
        payload = json.loads(raw, parse_float=Decimal)
        chart = payload.get('chart') if isinstance(payload, dict) else None
        if not isinstance(chart, dict) or chart.get('error'):
            raise ValueError('provider_chart_error')
        rows = chart.get('result')
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError('provider_result_empty_or_ambiguous')
        meta = rows[0].get('meta')
        if not isinstance(meta, dict) or not meta:
            raise ValueError('provider_metadata_empty')
        # Archive successful public chart JSON, including invalid market data for
        # an audit. Do not save HTTP denial/error pages with possible identifiers.
        out['raw_archive'], out['raw_sha256'] = _archive(raw, archive_dir)
        out.update(name=meta.get('longName') or meta.get('shortName'),
                   currency=meta.get('currency'), exchange=meta.get('exchangeName'),
                   exchange_timezone=meta.get('exchangeTimezoneName'),
                   instrument_type=meta.get('instrumentType'),
                   provider_market_state=meta.get('marketState'))
        if meta.get('symbol') != ticker:
            out['source_errors'].append('provider_ticker_mismatch')
        out['price'] = _price(meta.get('regularMarketPrice'))
        out['quote_at'] = _iso(_epoch(meta.get('regularMarketTime')))
        reported_delay = meta.get('exchangeDataDelayedBy')
        if reported_delay is None:
            out['delay'] = 'timestamp_fresh_delay_unverified'
        elif not isinstance(reported_delay, bool) and isinstance(reported_delay, (int, Decimal)) and reported_delay >= 0:
            out['delay_seconds'] = int(reported_delay) * 60
            out['delay'] = 'provider_reported_realtime' if reported_delay == 0 else 'delayed'
        else:
            out['source_errors'].append('provider_delay_invalid')
        period = (meta.get('currentTradingPeriod') or {}).get('regular') or {}
        out.update(provider_session_open=_iso(_epoch(period.get('start'))),
                   provider_session_close=_iso(_epoch(period.get('end'))))
        # A last-trade timestamp cannot authenticate a bid/ask timestamp. Only
        # preserve quotes with their own explicit time if the provider supplies it.
        if all(meta.get(key) is not None for key in ('bid', 'ask', 'bidTime', 'askTime')):
            out.update(bid=_price(meta['bid']), ask=_price(meta['ask']),
                       bid_at=_iso(_epoch(meta['bidTime'])), ask_at=_iso(_epoch(meta['askTime'])))
        out['corporate_actions'] = _actions(rows[0])
    except urllib.error.HTTPError as exc:
        out['source_errors'].append('provider_http_' + str(exc.code))
        exc.close()
    except (urllib.error.URLError, TimeoutError, OSError):
        out['source_errors'].append('provider_network_or_archive_error')
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        # Only controlled codes escape; provider exception text could contain a
        # URL, response body or other unexpected material.
        out['source_errors'].append('provider_response_invalid')
    return validate_quote(out, ticker=ticker, now=now)
