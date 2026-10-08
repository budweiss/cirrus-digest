"""Append-only accounting for the independent $200,000 paper simulation.

This module has no broker, credential, funding, reset, or real-order interface.
Money is integer cents; fills use Decimal and a disclosed adverse 5bp model.
The account can spend only settled simulated cash. Sale proceeds are reserved
until 16:00 New York on the next trading/banking day (deliberately conservative).

The tax export is a simulated transaction worksheet, NOT a return or a tax
calculation. FIFO gains are unadjusted; possible wash sales and incomplete
30-day windows are flagged, never used to prevent a risk exit.
References: https://www.sec.gov/exams/educationhelpguidesfaqs/t1-faq
            https://www.irs.gov/publications/p550
"""

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import urlsplit, parse_qsl
from zoneinfo import ZoneInfo

from stock_paper_market import SOURCE, market_session, next_trading_day, quote_url, validate_mark, validate_quote


ACCOUNT_ID = "independent-200k-v1"
INITIAL_CASH_CENTS = 20_000_000
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
SLIPPAGE_BPS = 5
TAX_NOTICE = (
    "SIMULATED / NOT FOR FILING. FIFO cost and proceeds are unadjusted paper "
    "amounts, not real taxable transactions or a tax-liability calculation. "
    "Possible same-symbol wash sales are flagged without basis adjustments. "
    "Substantially identical securities and activity in other accounts are not "
    "assessed. Corporate actions and distributions are unsupported and require "
    "an explicit accounting hold."
)


class LedgerError(ValueError):
    """A request would violate the paper account's accounting rules."""


class IdempotencyConflict(LedgerError):
    """An existing key was reused with different input."""


def _dt(value, label="timestamp"):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise LedgerError(label + " must be an ISO timestamp") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LedgerError(label + " must include a timezone")
    return value.astimezone(UTC)


def _iso(value):
    return _dt(value).isoformat(timespec="microseconds")


def _decimal(value, label, positive=False):
    if not isinstance(value, (str, Decimal)):
        raise LedgerError(label + " must be a decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise LedgerError(label + " must be a decimal string") from None
    if not number.is_finite() or number < 0 or number > Decimal("1000000000"):
        raise LedgerError(label + " is outside the supported range")
    if positive and number <= 0:
        raise LedgerError(label + " must be positive")
    return number


def _cents(value, label="money"):
    number = _decimal(value, label)
    cents = number * 100
    if cents != cents.to_integral_value():
        raise LedgerError(label + " must be an exact number of cents")
    return int(cents)


def _amount(cents):
    return format(Decimal(cents) / 100, ".2f")


def _ticker(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", value):
        raise LedgerError("ticker must be an uppercase US stock symbol")
    return value


def _text(value, label, limit=20000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise LedgerError(label + " must be nonempty text within its length limit")
    return value


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise LedgerError("payload must be finite JSON data") from None


def _payload(value):
    if not isinstance(value, dict):
        raise LedgerError("payload must be an object")
    return json.loads(_json(value))


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _key(payload):
    return _text(payload.get("idempotency_key"), "idempotency_key", 200)


def _id(kind, key):
    return kind + "_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _source_url(value):
    value = _text(value, "source URL", 2000)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise LedgerError("source URL must be public HTTPS without credentials")
    if any(re.search(r"token|key|secret|password|signature|credential", name, re.I)
           for name, _ in parse_qsl(parsed.query)):
        raise LedgerError("source URL must not contain credentials")
    return value


def fill_price(quote, side):
    """Return the exact 4-decimal adverse 5bp simulated fill price.

    The caller must validate the quote first. execute_trade always does so
    independently; this helper cannot make an unverified quote executable.
    """
    if side not in ("BUY", "SELL"):
        raise LedgerError("side must be BUY or SELL")
    field = "ask" if side == "BUY" else "bid"
    base = quote.get(field)
    if base is None:
        base = quote.get("price")
    number = _decimal(base, "quote price", positive=True)
    multiplier = Decimal("1.0005") if side == "BUY" else Decimal("0.9995")
    result = (number * multiplier).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    if result <= 0:
        raise LedgerError("simulated fill rounds to zero")
    return format(result, ".4f")


def _ny_date(value):
    return _dt(value).astimezone(NY).date()


def _extra_bank_holiday(day):
    # NYSE trades on these federal banking holidays. Reserving through the next
    # joint business day is conservative; this is not a broker settlement claim.
    columbus = date(day.year, 10, 1)
    columbus += timedelta(days=(0 - columbus.weekday()) % 7 + 7)
    veterans = date(day.year, 11, 11)
    if veterans.weekday() == 5:
        veterans -= timedelta(days=1)
    elif veterans.weekday() == 6:
        veterans += timedelta(days=1)
    return day in (columbus, veterans)


def _settlement_at(executed_at):
    day = next_trading_day(_ny_date(executed_at))
    while _extra_bank_holiday(day):
        day = next_trading_day(day)
    return datetime.combine(day, time(16, 0), NY).astimezone(UTC)


def _term(acquired_at, sold_at):
    acquired, sold = _ny_date(acquired_at), _ny_date(sold_at)
    try:
        anniversary = acquired.replace(year=acquired.year + 1)
    except ValueError:  # February 29: March 1 is the first date beyond one year.
        anniversary = acquired.replace(year=acquired.year + 1, day=28)
    return "LONG" if sold > anniversary else "SHORT"


class Ledger:
    """One isolated simulation account in a private SQLite file.

    ``now`` may be an aware datetime or callable for deterministic test fixtures.
    Production callers should omit it. No caller-supplied trade timestamp is
    accepted as the clock used for freshness checks.
    """

    def __init__(self, path, now=None):
        self.path = str(path)
        self._clock = now
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if Path(self.path).is_symlink():
                raise LedgerError("ledger path must not be a symlink")
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        self._conn = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_schema()

    def _now(self):
        value = self._clock() if callable(self._clock) else self._clock
        return _dt(value if value is not None else datetime.now(UTC), "clock")

    def close(self):
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    @contextmanager
    def _transaction(self, write=True):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def _create_schema(self):
        statements = [
            """CREATE TABLE IF NOT EXISTS account (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                account_id TEXT NOT NULL UNIQUE,
                initial_cash_cents INTEGER NOT NULL CHECK(initial_cash_cents=20000000),
                created_at TEXT NOT NULL, result_json TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS decisions (
                decision_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                payload_hash TEXT NOT NULL, decided_at TEXT NOT NULL,
                ticker TEXT, action TEXT NOT NULL, result_json TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                payload_hash TEXT NOT NULL, decision_id TEXT NOT NULL REFERENCES decisions,
                ticker TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                shares INTEGER NOT NULL CHECK(shares>0), price TEXT NOT NULL,
                gross_cents INTEGER NOT NULL CHECK(gross_cents>0),
                fees_cents INTEGER NOT NULL CHECK(fees_cents>=0),
                cash_delta_cents INTEGER NOT NULL,
                executed_at TEXT NOT NULL, settlement_at TEXT NOT NULL,
                result_json TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS lots (
                lot_id TEXT PRIMARY KEY, buy_trade_id TEXT NOT NULL UNIQUE REFERENCES trades,
                ticker TEXT NOT NULL, shares INTEGER NOT NULL CHECK(shares>0),
                cost_cents INTEGER NOT NULL CHECK(cost_cents>0), acquired_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS dispositions (
                disposition_id TEXT PRIMARY KEY, sale_trade_id TEXT NOT NULL REFERENCES trades,
                lot_id TEXT NOT NULL REFERENCES lots, ticker TEXT NOT NULL,
                shares INTEGER NOT NULL CHECK(shares>0), cost_cents INTEGER NOT NULL,
                proceeds_cents INTEGER NOT NULL, realized_cents INTEGER NOT NULL,
                acquired_at TEXT NOT NULL, sold_at TEXT NOT NULL,
                holding_term TEXT NOT NULL CHECK(holding_term IN ('SHORT','LONG')),
                UNIQUE(sale_trade_id,lot_id))""",
            """CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                payload_hash TEXT NOT NULL, kind TEXT NOT NULL,
                ticker TEXT NOT NULL, effective_at TEXT NOT NULL, result_json TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                payload_hash TEXT NOT NULL, recorded_at TEXT NOT NULL,
                result_json TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS trades_symbol_time ON trades(ticker,executed_at)",
            "CREATE INDEX IF NOT EXISTS disposals_symbol_time ON dispositions(ticker,sold_at)",
        ]
        with self._transaction() as conn:
            for statement in statements:
                conn.execute(statement)
            for table in ("account", "decisions", "trades", "lots", "dispositions", "events", "snapshots"):
                for operation in ("UPDATE", "DELETE"):
                    conn.execute(
                        "CREATE TRIGGER IF NOT EXISTS immutable_{0}_{1} "
                        "BEFORE {1} ON {0} BEGIN SELECT RAISE(ABORT, 'append-only ledger'); END".format(
                            table, operation))

    @staticmethod
    def _account(conn):
        row = conn.execute("SELECT * FROM account WHERE singleton=1").fetchone()
        if row is None:
            raise LedgerError("initialize the independent account before recording activity")
        return row

    @staticmethod
    def _replay(conn, table, key, payload_hash):
        row = conn.execute("SELECT payload_hash,result_json FROM " + table +
                           " WHERE idempotency_key=?", (key,)).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != payload_hash:
            raise IdempotencyConflict("idempotency key already belongs to different input")
        return json.loads(row["result_json"])

    def initialize(self, account_id=ACCOUNT_ID, initial_cash="200000.00"):
        if account_id != ACCOUNT_ID or _cents(initial_cash, "initial cash") != INITIAL_CASH_CENTS:
            raise LedgerError("this ledger supports only the fresh independent-200k-v1 account with $200,000")
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM account WHERE singleton=1").fetchone()
            if existing:
                if existing["account_id"] != account_id:
                    raise LedgerError("an existing account cannot be reset or replaced")
                return json.loads(existing["result_json"])
            result = {"account_id": account_id, "initial_cash_cents": INITIAL_CASH_CENTS,
                      "initial_cash": "200000.00", "currency": "USD", "simulated": True,
                      "created_at": _iso(self._now())}
            conn.execute("INSERT INTO account VALUES(1,?,?,?,?)",
                         (account_id, INITIAL_CASH_CENTS, result["created_at"], _json(result)))
            return result

    def record_decision(self, payload):
        data = _payload(payload)
        key, digest = _key(data), _digest(data)
        with self._transaction() as conn:
            self._account(conn)
            replay = self._replay(conn, "decisions", key, digest)
            if replay is not None:
                return replay
            action = data.get("action")
            if action not in ("BUY", "SELL", "HOLD"):
                raise LedgerError("decision action must be BUY, SELL, or HOLD")
            ticker = _ticker(data["ticker"]) if data.get("ticker") else None
            if action != "HOLD" and ticker is None:
                raise LedgerError("a trade decision needs a ticker")
            _text(data.get("reason"), "decision reason")
            now = self._now()
            decided = _dt(data.get("decided_at", now), "decided_at")
            if decided > now + timedelta(seconds=5):
                raise LedgerError("decision timestamp is in the future")
            result = dict(data, decision_id=_id("decision", key), decided_at=_iso(decided),
                          recorded_at=_iso(now), simulated=True)
            conn.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",
                         (result["decision_id"], key, digest, result["decided_at"],
                          ticker, action, _json(result)))
            return result

    @staticmethod
    def _balances(conn, now):
        account = Ledger._account(conn)
        row = conn.execute("""SELECT COALESCE(SUM(cash_delta_cents),0) AS delta,
            COALESCE(SUM(CASE WHEN side='SELL' AND settlement_at>? THEN cash_delta_cents ELSE 0 END),0)
            AS unsettled FROM trades""", (_iso(now),)).fetchone()
        cash = account["initial_cash_cents"] + row["delta"]
        return cash, cash - row["unsettled"], row["unsettled"]

    @staticmethod
    def _lots(conn, ticker=None):
        where, params = (" WHERE l.ticker=?", (ticker,)) if ticker else ("", ())
        return [dict(row) for row in conn.execute("""SELECT l.*,
            l.shares-COALESCE(SUM(d.shares),0) AS remaining_shares,
            l.cost_cents-COALESCE(SUM(d.cost_cents),0) AS remaining_cost_cents,
            COALESCE(SUM(d.shares),0) AS sold_shares,
            COALESCE(SUM(d.cost_cents),0) AS sold_cost_cents
            FROM lots l LEFT JOIN dispositions d ON l.lot_id=d.lot_id""" + where +
            " GROUP BY l.lot_id HAVING remaining_shares>0 ORDER BY l.acquired_at,l.rowid", params)]

    @staticmethod
    def _holds(conn, now):
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT result_json FROM events WHERE effective_at<=? ORDER BY effective_at,rowid", (_iso(now),))]

    def execute_trade(self, payload):
        data = _payload(payload)
        key, digest = _key(data), _digest(data)
        with self._transaction(write=False) as conn:
            self._account(conn)
            replay = self._replay(conn, "trades", key, digest)
            if replay is not None:
                return replay
        # A source-backed hold must survive the rejected fill's rollback.
        # Observe first in its own append-only transaction; no price is trusted.
        if isinstance(data.get("quote"), dict) and isinstance(data.get("ticker"), str):
            self.observe_quotes({data["ticker"]: data["quote"]})
        with self._transaction() as conn:
            self._account(conn)
            replay = self._replay(conn, "trades", key, digest)
            if replay is not None:
                return replay
            ticker = _ticker(data.get("ticker"))
            side = data.get("side")
            if side not in ("BUY", "SELL"):
                raise LedgerError("side must be BUY or SELL")
            shares = data.get("shares")
            if isinstance(shares, bool) or not isinstance(shares, int) or not 0 < shares <= 100_000_000:
                raise LedgerError("shares must be a positive whole-share count")
            if data.get("currency", "USD") != "USD":
                raise LedgerError("only USD trades are supported")
            _text(data.get("reason"), "trade reason")
            now = self._now()
            executed = _dt(data.get("executed_at"), "executed_at")
            age = (now - executed).total_seconds()
            if age > 120 or age < -5:
                raise LedgerError("execution timestamp is stale or in the future")
            # Independent local guard, even if an upstream executable flag lies.
            local = executed.astimezone(NY)
            if local.weekday() >= 5 or not time(9, 30) <= local.time() < time(16):
                raise LedgerError("execution is outside regular New York market hours")
            if market_session(now=now).get("state") != "REGULAR" or market_session(now=executed).get("state") != "REGULAR":
                raise LedgerError("regular trading session is not verified")
            last = conn.execute("SELECT MAX(executed_at) FROM trades").fetchone()[0]
            if last and executed < _dt(last):
                raise LedgerError("historical fills cannot be inserted behind newer trades")
            decision = conn.execute("SELECT * FROM decisions WHERE decision_id=?", (data.get("decision_id"),)).fetchone()
            if decision is None or decision["ticker"] != ticker or decision["action"] != side:
                raise LedgerError("trade must match a recorded decision's ticker and action")
            if _dt(decision["decided_at"]) > executed:
                raise LedgerError("trade cannot precede its decision")
            if any(event["ticker"] == ticker for event in self._holds(conn, now)):
                raise LedgerError("ticker has an unresolved corporate-action accounting hold")
            quote = data.get("quote")
            if not isinstance(quote, dict):
                raise LedgerError("trade requires source-backed quote evidence")
            try:
                checked = validate_quote(quote, ticker=ticker, side=side, now=now)
            except (ValueError, TypeError, KeyError):
                raise LedgerError("quote evidence failed validation") from None
            if not checked.get("executable"):
                raise LedgerError("quote is not executable: " + "; ".join(str(r) for r in checked.get("reasons", [])))
            price = _decimal(data.get("price"), "price", positive=True)
            if price != Decimal(fill_price(checked, side)):
                raise LedgerError("price does not match the verified adverse-5bp simulated fill")
            fees = _cents(data.get("fees", "0.00"), "fees")
            gross = int((price * shares * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            if gross <= 0 or (side == "SELL" and fees >= gross):
                raise LedgerError("trade proceeds must remain positive after fees")
            if gross + fees > 9_000_000_000_000_000_000:
                raise LedgerError("trade value exceeds supported integer accounting range")
            cash, settled, _ = self._balances(conn, now)
            lots = self._lots(conn, ticker)
            if side == "BUY" and gross + fees > settled:
                raise LedgerError("purchase exceeds settled self-financed cash")
            if side == "SELL" and shares > sum(lot["remaining_shares"] for lot in lots):
                raise LedgerError("sale exceeds shares owned; short sales are prohibited")
            trade_id = _id("trade", key)
            delta = -(gross + fees) if side == "BUY" else gross - fees
            try:
                settlement_at = _iso(_settlement_at(executed))
            except ValueError:
                raise LedgerError("settlement calendar is not verified for this trade") from None
            dispositions = []
            if side == "SELL":
                left, cumulative, allocated = shares, 0, 0
                for lot in lots:
                    take = min(left, lot["remaining_shares"])
                    if not take:
                        break
                    # Cumulative integer allocation guarantees pennies are conserved
                    # across arbitrary partial sales and multiple FIFO lots.
                    cost = (lot["cost_cents"] * (lot["sold_shares"] + take) // lot["shares"]
                            - lot["sold_cost_cents"])
                    cumulative += take
                    proceeds = delta * cumulative // shares - allocated
                    allocated += proceeds
                    dispositions.append({"disposition_id": trade_id + ":" + lot["lot_id"],
                                         "sale_trade_id": trade_id, "lot_id": lot["lot_id"],
                                         "ticker": ticker, "shares": take, "cost_cents": cost,
                                         "proceeds_cents": proceeds, "realized_cents": proceeds - cost,
                                         "acquired_at": lot["acquired_at"], "sold_at": _iso(executed),
                                         "holding_term": _term(lot["acquired_at"], executed)})
                    left -= take
                    if left == 0:
                        break
            result = dict(data, trade_id=trade_id, currency="USD", simulated=True,
                          price=format(price, ".4f"), executed_at=_iso(executed),
                          recorded_at=_iso(now), settlement_at=settlement_at,
                          quote=checked, quote_at=checked.get("quote_at"),
                          quote_source=checked.get("source"), quote_url=checked.get("url"),
                          fill_model="adverse_5bp_" + ("ask" if side == "BUY" else "bid")
                          if checked.get("ask" if side == "BUY" else "bid") is not None
                          else "adverse_5bp_last_trade_simulation",
                          slippage_bps=SLIPPAGE_BPS, gross_cents=gross, gross=_amount(gross),
                          fees_cents=fees, fees=_amount(fees), cash_delta_cents=delta,
                          cash_after_cents=cash + delta, cash_after=_amount(cash + delta),
                          realized_pnl_cents=sum(d["realized_cents"] for d in dispositions),
                          dispositions=dispositions)
            conn.execute("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (trade_id, key, digest, data["decision_id"], ticker, side, shares,
                          result["price"], gross, fees, delta, result["executed_at"],
                          settlement_at, _json(result)))
            if side == "BUY":
                conn.execute("INSERT INTO lots VALUES(?,?,?,?,?,?)",
                             ("lot_" + trade_id, trade_id, ticker, shares, gross + fees, result["executed_at"]))
            else:
                for d in dispositions:
                    conn.execute("INSERT INTO dispositions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                 tuple(d[k] for k in ("disposition_id", "sale_trade_id", "lot_id", "ticker", "shares",
                                                     "cost_cents", "proceeds_cents", "realized_cents", "acquired_at",
                                                     "sold_at", "holding_term")))
            return result

    def record_event(self, payload):
        """Append a source-backed hold for an unsupported dividend/split/action.

        There is intentionally no cash-credit, position rewrite, or clear-hold
        event until the corresponding accounting is implemented and verified.
        """
        data = _payload(payload)
        key, digest = _key(data), _digest(data)
        with self._transaction() as conn:
            self._account(conn)
            replay = self._replay(conn, "events", key, digest)
            if replay is not None:
                return replay
            if data.get("kind") != "CORPORATE_ACTION_HOLD":
                raise LedgerError("only CORPORATE_ACTION_HOLD events are supported; no topups or resets")
            ticker = _ticker(data.get("ticker"))
            _text(data.get("reason"), "event reason")
            _text(data.get("source"), "event source", 200)
            _source_url(data.get("url"))
            effective = _dt(data.get("effective_at"), "effective_at")
            result = dict(data, event_id=_id("event", key), effective_at=_iso(effective),
                          recorded_at=_iso(self._now()), simulated=True, unresolved=True)
            conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                         (result["event_id"], key, digest, data["kind"], ticker,
                          result["effective_at"], _json(result)))
            return result

    def observe_quotes(self, quotes):
        """Retain detected action holds after the provider's short history expires.

        Dividend/split entitlement is evaluated from shares owned before the
        action's New York date. A historical action before any exposure does
        not affect this fresh account. These events are holds, not cash credits.
        """
        if not isinstance(quotes, dict):
            raise LedgerError("quotes must be a ticker-to-quote mapping")
        recorded = []
        for ticker, quote in quotes.items():
            if not isinstance(quote, dict) or not quote.get("corporate_actions"):
                continue
            try:
                _ticker(ticker)
                source_matches = (quote.get("ticker") == ticker and quote.get("source") == SOURCE
                                  and quote.get("url") == quote_url(ticker))
            except ValueError:
                continue
            if not source_matches:
                continue
            actions = quote["corporate_actions"]
            if not isinstance(actions, list):
                raise LedgerError("corporate-action evidence is malformed")
            for action in actions:
                if not isinstance(action, dict) or action.get("kind") not in ("dividend", "split"):
                    raise LedgerError("corporate-action evidence is malformed")
                action_at = _dt(action.get("at"), "corporate-action time")
                if _ny_date(action_at) > _ny_date(self._now()):
                    continue  # The future entitlement is not yet known.
                cutoff = _iso(datetime.combine(_ny_date(action_at), time.min, NY))
                with self._transaction(write=False) as conn:
                    self._account(conn)
                    exposed = conn.execute("""SELECT COALESCE(SUM(CASE WHEN side='BUY'
                        THEN shares ELSE -shares END),0) FROM trades WHERE ticker=? AND executed_at<?""",
                        (ticker, cutoff)).fetchone()[0]
                if exposed <= 0:
                    continue
                recorded.append(self.record_event({
                    "idempotency_key": "detected-action:" + _digest({"ticker": ticker, "action": action}),
                    "kind": "CORPORATE_ACTION_HOLD", "ticker": ticker,
                    "effective_at": cutoff, "source": SOURCE, "url": quote["url"],
                    "reason": "Source reports a " + action["kind"] + "; source-reviewed accounting is required",
                    "source_action": action, "shares_before_action_date": exposed,
                }))
        return recorded

    def _snapshot(self, conn, quotes, now):
        account = self._account(conn)
        cash, settled, unsettled = self._balances(conn, now)
        lots = self._lots(conn)
        holds = self._holds(conn, now)
        held_symbols = {event["ticker"] for event in holds}
        positions = {}
        for lot in lots:
            pos = positions.setdefault(lot["ticker"], {"ticker": lot["ticker"], "shares": 0,
                                                       "cost_basis_cents": 0, "lots": []})
            pos["shares"] += lot["remaining_shares"]
            pos["cost_basis_cents"] += lot["remaining_cost_cents"]
            pos["lots"].append(lot)
        priced_value, unrealized, unpriced = 0, 0, []
        for ticker, pos in sorted(positions.items()):
            quote = quotes.get(ticker) if isinstance(quotes, dict) else None
            checked, reasons = None, []
            if ticker in held_symbols:
                reasons = ["unresolved corporate-action accounting hold"]
            elif not isinstance(quote, dict):
                reasons = ["fresh quote missing"]
            else:
                try:
                    checked = validate_mark(quote, now=now)
                    if checked.get("ticker") != ticker:
                        reasons = ["ticker mismatch"]
                    elif not checked.get("valuation_eligible"):
                        reasons = list(checked.get("valuation_reasons") or ["fresh quote unavailable"])
                except (ValueError, TypeError, KeyError):
                    reasons = ["quote validation failed"]
            value = None
            if not reasons:
                try:
                    price = _decimal(checked.get("price"), "valuation price", positive=True)
                    value = int((price * pos["shares"] * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
                except LedgerError:
                    reasons = ["valuation price invalid"]
            if value is None:
                unpriced.append(ticker)
            else:
                priced_value += value
                unrealized += value - pos["cost_basis_cents"]
            pos.update(cost_basis=_amount(pos["cost_basis_cents"]),
                       market_price=checked.get("price") if value is not None else None,
                       market_value_cents=value, market_value=_amount(value) if value is not None else None,
                       unrealized_pnl_cents=value - pos["cost_basis_cents"] if value is not None else None,
                       quote_at=checked.get("quote_at") if checked else None,
                       quote_source=checked.get("source") if checked else None,
                       quote_delay=checked.get("delay") if checked else None,
                       valuation_label=checked.get("valuation_label") if checked and value is not None else None,
                       valuation_session_date=checked.get("valuation_session_date") if checked and value is not None else None,
                       unpriced_reasons=reasons)
        realized = conn.execute("SELECT COALESCE(SUM(realized_cents),0) FROM dispositions").fetchone()[0]
        trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        last_trade = conn.execute("SELECT trade_id FROM trades ORDER BY rowid DESC LIMIT 1").fetchone()
        event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        last_event = conn.execute("SELECT event_id FROM events ORDER BY rowid DESC LIMIT 1").fetchone()
        # A hold also invalidates totals after a position was liquidated: an
        # unbooked distribution must not disappear merely because shares are zero.
        complete = not unpriced and not holds
        equity = cash + priced_value if complete else None
        result = {"account_id": account["account_id"], "simulated": True, "currency": "USD",
                  "as_of": _iso(now), "initial_cash_cents": INITIAL_CASH_CENTS,
                  "initial_cash": _amount(INITIAL_CASH_CENTS), "cash_cents": cash, "cash": _amount(cash),
                  "settled_cash_cents": settled, "settled_cash": _amount(settled),
                  "unsettled_cash_cents": unsettled, "unsettled_cash": _amount(unsettled),
                  "positions": list(sorted(positions.values(), key=lambda item: item["ticker"])),
                  "priced_market_value_cents": priced_value, "unpriced_tickers": unpriced,
                  "valuation_complete": complete, "equity_cents": equity,
                  "equity": _amount(equity) if equity is not None else None,
                  "realized_pnl_cents": realized, "realized_pnl": _amount(realized),
                  "unrealized_pnl_cents": unrealized if complete else None,
                  "unrealized_pnl": _amount(unrealized) if complete else None,
                  "total_pnl_cents": equity - INITIAL_CASH_CENTS if equity is not None else None,
                  "total_pnl": _amount(equity - INITIAL_CASH_CENTS) if equity is not None else None,
                  "corporate_action_holds": holds,
                  "trade_count": trade_count, "last_trade_id": last_trade[0] if last_trade else None,
                  "event_count": event_count, "last_event_id": last_event[0] if last_event else None,
                  "settlement_policy": "sale proceeds reserved through 16:00 ET next trading/banking day",
                  "fill_policy": "5bp adverse slippage; bid/ask when supplied, otherwise fresh last-trade simulation",
                  "tax_notice": TAX_NOTICE}
        return result

    def snapshot(self, quotes=None):
        self.observe_quotes(quotes or {})
        with self._transaction(write=False) as conn:
            return self._snapshot(conn, quotes or {}, self._now())

    def record_snapshot(self, quotes=None, *, idempotency_key):
        key = _text(idempotency_key, "idempotency_key", 200)
        data = {"quotes": quotes or {}}
        digest = _digest(data)
        with self._transaction(write=False) as conn:
            self._account(conn)
            replay = self._replay(conn, "snapshots", key, digest)
            if replay is not None:
                return replay
        self.observe_quotes(quotes or {})
        with self._transaction() as conn:
            self._account(conn)
            replay = self._replay(conn, "snapshots", key, digest)
            if replay is not None:
                return replay
            result = self._snapshot(conn, quotes or {}, self._now())
            result.update(snapshot_id=_id("snapshot", key), idempotency_key=key)
            conn.execute("INSERT INTO snapshots VALUES(?,?,?,?,?)",
                         (result["snapshot_id"], key, digest, result["as_of"], _json(result)))
            return result

    def _list(self, table, limit):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 10000:
            raise LedgerError("limit must be between 1 and 10000")
        with self._transaction(write=False) as conn:
            self._account(conn)
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT result_json FROM " + table + " ORDER BY rowid DESC LIMIT ?", (limit,))]

    def list_decisions(self, limit=100):
        return self._list("decisions", limit)

    def list_trades(self, limit=100):
        return self._list("trades", limit)

    def list_events(self, limit=100):
        return self._list("events", limit)

    def list_snapshots(self, limit=100):
        return self._list("snapshots", limit)

    def get_snapshot(self, idempotency_key):
        key = _text(idempotency_key, "idempotency_key", 200)
        with self._transaction(write=False) as conn:
            self._account(conn)
            row = conn.execute("SELECT result_json FROM snapshots WHERE idempotency_key=?", (key,)).fetchone()
            if row is None:
                raise LedgerError("recorded snapshot was not found")
            return json.loads(row[0])

    def export_tax(self, year):
        """Export actual simulated FIFO dispositions for the NY trade-date year.

        Future replacement purchases can change the wash review flags in a new
        export, but never rewrite the original trade, lot, or disposition.
        """
        if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 9998:
            raise LedgerError("year must be an integer from 2000 through 9998")
        with self._transaction(write=False) as conn:
            account = self._account(conn)
            now = self._now()
            buys = [dict(row) for row in conn.execute("SELECT * FROM trades WHERE side='BUY'")]
            trade_order = {row["trade_id"]: row["sequence"] for row in
                           conn.execute("SELECT trade_id,rowid AS sequence FROM trades")}
            sold_rows = [dict(row) for row in conn.execute("SELECT * FROM dispositions ORDER BY sold_at,rowid")]
            lots = {row["lot_id"]: dict(row) for row in conn.execute("SELECT * FROM lots")}
            output = []
            for row in sold_rows:
                sold_date = _ny_date(row["sold_at"])
                if sold_date.year != year:
                    continue
                window_end = sold_date + timedelta(days=30)
                matches = []
                if row["realized_cents"] < 0:
                    for buy in buys:
                        if buy["ticker"] != row["ticker"] or abs((_ny_date(buy["executed_at"]) - sold_date).days) > 30:
                            continue
                        source_lot = next((lot for lot in lots.values() if lot["buy_trade_id"] == buy["trade_id"]), None)
                        # Before-sale purchases count when replacement shares
                        # remain after this entire sale. The original acquisition
                        # fully disposed by this sale is not its own replacement.
                        disposed_by_sale = sum(d["shares"] for d in sold_rows
                                               if source_lot and d["lot_id"] == source_lot["lot_id"]
                                               and trade_order[d["sale_trade_id"]] <= trade_order[row["sale_trade_id"]])
                        after = trade_order[buy["trade_id"]] > trade_order[row["sale_trade_id"]]
                        if after or (source_lot and source_lot["shares"] > disposed_by_sale):
                            matches.append(buy["trade_id"])
                open_window = row["realized_cents"] < 0 and _ny_date(now) <= window_end
                item = dict(row, acquisition_date=_ny_date(row["acquired_at"]).isoformat(),
                            disposition_date=sold_date.isoformat(), cost_basis=_amount(row["cost_cents"]),
                            proceeds=_amount(row["proceeds_cents"]), realized_pnl=_amount(row["realized_cents"]),
                            possible_wash_sale=bool(matches), possible_replacement_trade_ids=matches,
                            wash_window_open=open_window, wash_window_ends=window_end.isoformat(),
                            tax_treatment="UNADJUSTED_REVIEW_REQUIRED" if matches or open_window else "UNADJUSTED_SIMULATION")
                output.append(item)
            short = sum(row["realized_cents"] for row in output if row["holding_term"] == "SHORT")
            long = sum(row["realized_cents"] for row in output if row["holding_term"] == "LONG")
            holds = self._holds(conn, now)
            return {"account_id": account["account_id"], "year": year, "simulated": True,
                    "not_for_filing": True, "notice": TAX_NOTICE, "generated_at": _iso(now),
                    "method": "FIFO, fees included, unadjusted cost basis", "dispositions": output,
                    "short_term_realized_cents": short, "short_term_realized": _amount(short),
                    "long_term_realized_cents": long, "long_term_realized": _amount(long),
                    "total_realized_cents": short + long, "total_realized": _amount(short + long),
                    "possible_wash_sale_count": sum(bool(row["possible_wash_sale"]) for row in output),
                    "open_wash_window_count": sum(bool(row["wash_window_open"]) for row in output),
                    "tax_review_required": bool(holds or any(row["possible_wash_sale"] or row["wash_window_open"] for row in output)),
                    "corporate_action_holds": holds,
                    "sources": ["https://www.irs.gov/publications/p550",
                                "https://www.sec.gov/exams/educationhelpguidesfaqs/t1-faq"]}
