#!/usr/bin/env python3
"""Private independent paper-account workflow. No brokerage or email access."""
import argparse
import csv
import fcntl
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_paper_ledger import Ledger, fill_price
from stock_paper_market import get_quote, market_session, validate_quote, validate_mark

HOME = Path(__file__).resolve().parent / 'private' / 'stock-paper'
NY = ZoneInfo('America/New_York')


def now():
    return datetime.now(timezone.utc)


def packed(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2)


def safe_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,150}', value):
        raise ValueError('invalid_stable_request_or_order_id')
    return value


def save(path, value, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if exclusive:
        with path.open('x') as out:
            out.write(packed(value) + '\n')
    else:
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        temporary.write_text(packed(value) + '\n')
        temporary.replace(path)


def dollars(cents):
    return 'unavailable' if cents is None else '${:,.2f}'.format(Decimal(cents) / 100)


def local_time(stamp):
    return datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(NY).strftime('%b %d %H:%M:%S ET')


def fetch_quotes(home, symbols):
    return {symbol: get_quote(symbol, archive_dir=home / 'quotes') for symbol in sorted(set(symbols))}


def delivery_receipts(home):
    records = []
    for path in sorted((home / 'delivery').glob('????-??-??-?m.json'), reverse=True)[:30]:
        records.append(dict(json.loads(path.read_text()), slot_key=path.stem))
    return records


def initialize(home, ledger):
    result = ledger.initialize()
    path = home / 'benchmark.json'
    if not path.exists():
        quote = get_quote('SPY', archive_dir=home / 'quotes')
        # Benchmark start is recorded prospectively, never reconstructed later.
        if quote.get('executable'):
            save(path, {'recorded_at': now().isoformat(), 'quote': quote,
                        'initial_value': '200000.00', 'comparison': 'SPY price return, zero interest cash; distributions provisional'}, True)
    return result


def benchmark(home, quote):
    path = home / 'benchmark.json'
    quote = validate_mark(quote, now=now())
    if not path.exists() or quote.get('ticker') != 'SPY' or not quote.get('valuation_eligible'):
        return {'available': False, 'reason': 'verified_start_or_current_reference_missing'}
    start = json.loads(path.read_text())
    change = Decimal(quote['price']) / Decimal(start['quote']['price']) - 1
    return {'available': True, 'return_percent': str((change * 100).quantize(Decimal('.01'))),
            'reference_value_cents': int((Decimal(20000000) * (1 + change)).quantize(Decimal('1'))),
            'started_at': start['recorded_at'], 'quote_at': quote['quote_at'],
            'valuation_label': quote['valuation_label'],
            'notice': 'SPY price-only reference; dividends/corporate actions not included; not a total-return skill claim.'}


def account_section(snapshot, trades, bench, slot):
    at = snapshot['as_of']
    lines = ['## Our simulated account', '',
             'Independent account; started with **$200,000.00**. Figures observed ' + local_time(at) + '.', '',
             '| Account measure | Amount |', '|---|---:|',
             '| Cash | ' + dollars(snapshot['cash_cents']) + ' |',
             '| Available to buy (settled cash) | ' + dollars(snapshot['settled_cash_cents']) + ' |',
             '| Sale proceeds still settling | ' + dollars(snapshot['unsettled_cash_cents']) + ' |',
             '| Shares plus cash | ' + dollars(snapshot['equity_cents']) + ' |',
             '| Gain/loss since starting | ' + dollars(snapshot['total_pnl_cents']) + ' |',
             '| Realized gain/loss on sold shares | ' + dollars(snapshot['realized_pnl_cents']) + ' |',
             '| Unrealized gain/loss on open shares | ' + dollars(snapshot['unrealized_pnl_cents']) + ' |', '']
    if snapshot['total_pnl_cents'] is not None:
        lines += ['Account return: **{:.2f}%**.'.format(Decimal(snapshot['total_pnl_cents']) / Decimal(200000)), '']
    if snapshot['positions']:
        lines += ['| Stock | Shares | Cost basis | Observed price | Value | Open gain/loss | Price as of |',
                  '|---|---:|---:|---:|---:|---:|---|']
        for p in snapshot['positions']:
            lines.append('| {ticker} | {shares} | {basis} | {price} | {value} | {gain} | {at} |'.format(
                ticker=p['ticker'], shares=p['shares'], basis=dollars(p['cost_basis_cents']),
                price=('$' + p['market_price']) if p['market_price'] else 'unavailable',
                value=dollars(p['market_value_cents']), gain=dollars(p['unrealized_pnl_cents']),
                at=local_time(p['quote_at']) if p['quote_at'] else 'missing'))
        lines.append('')
    else:
        lines += ['No shares owned yet; all capital is cash.', '']
    for p in snapshot['positions']:
        if p.get('valuation_label') and p['valuation_label'] != 'current_regular_trade':
            lines.append(p['ticker'] + ': ' + p['valuation_label'] + '; this is not a new executable price.')
        if p.get('unpriced_reasons'):
            lines.append(p['ticker'] + ': valuation incomplete — ' + ', '.join(p['unpriced_reasons']) + '.')
    today = datetime.fromisoformat(at).astimezone(NY).date()
    recent = [t for t in trades if datetime.fromisoformat(t['executed_at']).astimezone(NY).date() == today]
    lines += ['', '### Completed paper trades today', '']
    if recent:
        lines += ['| Time | Move | Shares | Simulated fill | Amount | Why |', '|---|---|---:|---:|---:|---|']
        for t in reversed(recent):
            reason = t['reason'].replace('|', '/').replace('\n', ' ')
            lines.append('| {} | {} {} | {} | ${} | {} | {} |'.format(
                local_time(t['executed_at']), t['side'], t['ticker'], t['shares'], t['price'], dollars(t['gross_cents']), reason))
    else:
        lines += ['No completed paper trades today. Pending ideas are not positions.']
    lines += ['', 'Fills use provider-timestamped public observations with a 0.05% adverse allowance per side. '
              'An unknown feed delay stays unknown; this is a paper model, not a guaranteed exchange fill.', '']
    if bench.get('available'):
        lines += ['SPY reference since ' + local_time(bench['started_at']) + ': **' + bench['return_percent'] + '%** (price only).', bench['notice'], '']
    else:
        lines += ['SPY comparison is unavailable until both matching reference prices are verified.', '']
    if snapshot.get('corporate_action_holds'):
        lines += ['**Corporate-action adjustment pending; account performance is provisional.**', '']
    lines += ['Year-end record: FIFO sale lots with costs/proceeds and short/long holding periods. '
              'Possible wash sales and unbooked corporate actions require review. '
              '**Simulation only: no actual taxable trades; not a tax filing.**', '']
    return '\n'.join(lines)


def refresh(home, ledger, slot):
    positions = ledger.snapshot()['positions']
    decisions = ledger.list_decisions(10000)
    latest_decisions = {}
    for decision in decisions:
        if decision.get('ticker') and decision['ticker'] not in latest_decisions:
            latest_decisions[decision['ticker']] = decision
    watch = list(latest_decisions)[:20]
    quotes = fetch_quotes(home, [p['ticker'] for p in positions] + watch + ['SPY'])
    report_id = uuid.uuid4().hex
    snapshot = ledger.record_snapshot(quotes, idempotency_key=report_id)
    trades = ledger.list_trades(10000)
    outcomes = []
    for decision in latest_decisions.values():
        order = decision.get('order')
        if order:
            path = home / 'orders' / (hashlib.sha256(order['idempotency_key'].encode()).hexdigest() + '.json')
            if path.exists():
                recorded = json.loads(path.read_text())
                outcomes.append(recorded.get('outcome') or {'ticker': decision['ticker'], 'status': 'outcome_unknown_inspect_same_order'})
    bench = benchmark(home, quotes['SPY'])
    section = account_section(snapshot, trades, bench, slot)
    marker = '<!-- stock-paper-report:' + report_id + ' -->'
    result = {'status': 'complete', 'account_id': snapshot['account_id'], 'report_id': report_id,
              'slot': slot, 'date': now().astimezone(NY).date().isoformat(),
              'created_at': snapshot['as_of'], 'snapshot': snapshot, 'benchmark': bench,
              'market': market_session(), 'quotes': quotes, 'trades': trades,
              'decisions': decisions, 'order_outcomes': outcomes, 'deliveries': delivery_receipts(home),
              'account_section': section, 'marker': marker,
              'report': marker + '\n\n' + section}
    save(home / 'reports' / (report_id + '.json'), result, True)
    save(home / 'latest.json', result)
    # The existing nightly file backup can copy this consistent database even
    # if the live SQLite WAL changes during its scan.
    backup = home / 'ledger-backup.tmp.sqlite3'
    with closing(sqlite3.connect(str(home / 'ledger.sqlite3'))) as source:
        with closing(sqlite3.connect(str(backup))) as destination:
            source.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('ledger_backup_integrity_failed')
    backup.replace(home / 'ledger-backup.sqlite3')
    return result


def validate_decision(data):
    safe_key(data.get('idempotency_key'))
    if data.get('action') not in ('BUY', 'SELL', 'HOLD'):
        raise ValueError('invalid_decision_action')
    for key in ('reason', 'strategy_version'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError('decision_needs_' + key)
    if data['action'] != 'HOLD':
        for key in ('valuation', 'counterargument', 'exit_trigger', 'review_dates'):
            if not data.get(key):
                raise ValueError('trade_decision_needs_' + key)
        evidence = data.get('evidence')
        if not isinstance(evidence, list) or not evidence:
            raise ValueError('trade_needs_dated_source_evidence')
        for item in evidence:
            from urllib.parse import urlsplit
            url = urlsplit(item.get('url', ''))
            if url.scheme != 'https' or not url.hostname or url.username or url.password:
                raise ValueError('public_https_evidence_required')
            if not item.get('published') or not item.get('claim'):
                raise ValueError('source_date_and_claim_required')
        order = data.get('order', {})
        safe_key(order.get('idempotency_key'))
        if set(order) - {'idempotency_key', 'shares', 'budget', 'limit_price'}:
            raise ValueError('unknown_order_fields_or_caller_fill_price')
        limit = Decimal(str(order.get('limit_price', '0')))
        if not limit.is_finite() or limit <= 0:
            raise ValueError('positive_price_limit_required')
        if data['action'] == 'SELL' and 'budget' in order:
            raise ValueError('sell_requires_share_quantity')
        if ('shares' in order) == ('budget' in order):
            raise ValueError('choose_exactly_one_shares_or_budget')


def apply(home, ledger, request):
    request_id = safe_key(request.get('request_id'))
    decisions = request.get('decisions')
    if not isinstance(decisions, list) or not 1 <= len(decisions) <= 20:
        raise ValueError('request_needs_1_to_20_decisions')
    for data in decisions:
        validate_decision(data)
    folder = home / 'requests' / request_id
    input_path, result_path = folder / 'input.json', folder / 'result.json'
    if input_path.exists():
        if json.loads(input_path.read_text()) != request:
            raise ValueError('request_id_reused_with_changed_instructions')
        if result_path.exists():
            return json.loads(result_path.read_text())
    else:
        save(input_path, request, True)
    results = []
    for data in decisions:
        decision = ledger.record_decision(data)
        outcome = {'decision_id': decision['decision_id'], 'ticker': decision.get('ticker'), 'action': data['action']}
        if data['action'] == 'HOLD':
            outcome.update(status='recorded', reason=data['reason'])
            results.append(outcome)
            continue
        order = data['order']
        order_path = home / 'orders' / (hashlib.sha256(order['idempotency_key'].encode()).hexdigest() + '.json')
        if order_path.exists():
            saved = json.loads(order_path.read_text())
            if saved['decision'] != data:
                raise ValueError('order_id_reused_with_changed_decision')
            if saved.get('outcome'):
                results.append(saved['outcome'])
                continue
        else:
            quote = get_quote(data['ticker'], archive_dir=home / 'quotes')
            if quote.get('instrument_type') != 'EQUITY':
                quote = dict(quote, executable=False, reasons=['common_stock_required'])
            saved = {'decision': data, 'quote': quote, 'recorded_at': now().isoformat()}
            if not quote.get('executable'):
                outcome.update(status='held', reason=', '.join(quote.get('reasons', ['quote_unavailable'])))
                saved['outcome'] = outcome
            else:
                price = Decimal(fill_price(quote, data['action']))
                limit = Decimal(str(order['limit_price']))
                outside = price > limit if data['action'] == 'BUY' else price < limit
                if outside:
                    outcome.update(status='held', reason='price_outside_limit', observed_price=str(price), limit_price=str(limit))
                    saved['outcome'] = outcome
                else:
                    if 'budget' in order:
                        budget = Decimal(str(order['budget']))
                        if not budget.is_finite() or budget <= 0:
                            raise ValueError('positive_finite_budget_required')
                        shares = int((budget / price).to_integral_value(rounding=ROUND_FLOOR))
                    else:
                        shares = order['shares']
                    saved['trade_payload'] = {'idempotency_key': order['idempotency_key'], 'decision_id': decision['decision_id'],
                        'ticker': data['ticker'], 'side': data['action'], 'shares': shares, 'price': str(price),
                        'fees': '0.00', 'executed_at': now().isoformat(), 'quote': quote, 'reason': data['reason']}
            save(order_path, saved, True)
        if not saved.get('outcome'):
            try:
                trade = ledger.execute_trade(saved['trade_payload'])
                outcome.update(status='filled', trade=trade)
            except ValueError as exc:
                outcome.update(status='held', reason=str(exc))
            saved['outcome'] = outcome
            save(order_path, saved)
        results.append(saved['outcome'])
    result = {'status': 'complete', 'request_id': request_id, 'outcomes': results, 'finished_at': now().isoformat()}
    save(result_path, result, True)
    return result


def tax_export(home, ledger, year):
    result = ledger.export_tax(year)
    save(home / 'tax' / (str(year) + '-latest.json'), result)
    columns = ['ticker', 'shares', 'acquisition_date', 'disposition_date', 'cost_basis', 'proceeds',
               'realized_pnl', 'holding_term', 'possible_wash_sale', 'wash_window_open', 'tax_treatment']
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(result['dispositions'])
    result['csv'] = out.getvalue()
    (home / 'tax' / (str(year) + '-latest.csv')).write_text(result['csv'])
    return result


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=HOME)
    parser.add_argument('action', choices=('init', 'status', 'refresh', 'apply', 'tax-export', 'research-config'))
    parser.add_argument('--slot', choices=('am', 'pm', 'trial'), default='trial')
    parser.add_argument('--year', type=int)
    args = parser.parse_args(argv)
    home = args.home
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)
    try:
        with (home / 'operation.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with Ledger(home / 'ledger.sqlite3') as ledger:
                if args.action == 'init':
                    result = initialize(home, ledger)
                elif args.action == 'status':
                    result = {'status': 'ok', 'snapshot': ledger.snapshot(), 'market': market_session(),
                              'trades': ledger.list_trades(10000), 'decisions': ledger.list_decisions(10000),
                              'deliveries': delivery_receipts(home)}
                elif args.action == 'refresh':
                    result = refresh(home, ledger, args.slot)
                elif args.action == 'apply':
                    result = apply(home, ledger, json.load(sys.stdin))
                elif args.action == 'tax-export':
                    result = tax_export(home, ledger, args.year or now().astimezone(NY).year)
                else:
                    from stock_research import validate_manifest, database
                    manifest = validate_manifest(json.load(sys.stdin))
                    folder = home / 'research'
                    database(folder).close()
                    path = folder / 'universe.json'
                    if path.exists():
                        old = json.loads(path.read_text())
                        save(folder / 'universe-history' / (uuid.uuid4().hex + '.json'), old, True)
                    save(path, manifest)
                    result = {'status': 'configured', 'companies': len(manifest['companies'])}
        print(packed(result))
        return 0
    except Exception as exc:
        # Do not print transport bodies/configs. Domain errors contain controlled data.
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(packed({'status': 'failed', 'reason': reason}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
