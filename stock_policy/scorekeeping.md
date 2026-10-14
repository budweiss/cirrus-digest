# Independent paper-account scorekeeping

Active October 8, 2026. This supersedes the old $1,000 experiment/calls-CSV
scoring method for the clean $200,000 account. Preserve historical files;
their entries and returns are excluded from this account.

## Money and positions

The CUMULUS1 ledger is authoritative. One $200,000 opening cash balance,
no deposits/resets/borrowing, whole shares, owned-share sales only. Book buys
and sales from current provider-stamped quotes, with 0.05% adverse slippage.
Use integer cents for money and FIFO cost lots. Cash plus positions is account
value; realized gains/losses are only closed lots. Unrealized gains/losses
compare owned-share observed value with remaining cost basis. Sale proceeds
are reserved until the next trading/banking day at 16:00 ET (conservative T+1).
Cash earns zero. No income from imaginary interest or unverified dividends.

Reject stale/future/missing/explicitly delayed quotes for fills. The provider's
last trade is an observation, not a guaranteed executable bid/ask. Unknown
feed delay is shown explicitly. When the market is closed, a verified last
regular-session observation may value positions with its real date/time;
it never permits a new fill. Missing valuation makes the total incomplete.

Orders and decisions have stable unique IDs. Same ID/same instructions retrieves
the result; different instructions cannot reuse the ID. Do not replace a
failed/losing trade with a different entry price. Preserve all holds/rejections.

## Prospective decisions and review

Before each order, freeze business thesis, valuation, sources/dates,
counterargument, sell conditions, strategy version and 21/28 calendar-day
review dates (next trading day for a closed date). BUY/SELL/HOLD is a managed
account decision, distinct from an outside commentator's opinion. A repeat
HOLD is not a new independent successful pick. Link later changed decisions
to the original; never rewrite what was known at entry.

Every daily recap shows actual holdings/cash/actions. Weekly reviews assess:
account change from $200,000; realized/unrealized gains; cash left idle; costs;
price-only SPY reference recorded at account initiation; and the dated original
21/28-day judgments. Preserve fixed-window observation of a sold idea separately
from actual managed-account performance. Do not backfill a missing initial
benchmark using hindsight. Total-return comparisons require verified dividends
and splits; the initial SPY reference is explicitly price-only.

Keep dated snapshots, maximum observed peak-to-trough loss and number of
completed, distinct ideas. Twice-daily observations cannot prove intraday
maximum drawdown. Do not count several correlated stocks as independent proof.
Explain whether business reasoning held up separately from share-price luck.
Classify misses as wrong facts, valuation, timing, business change or broad
market moves. Changes get a new version tested on subsequent decisions.
No optimizing on the same winners and calling that an independent test.

## Corporate actions and year-end records

The initial implementation detects source-reported dividends/splits during
ownership and persists an accounting hold. It does not silently credit cash,
adjust shares or manufacture tax basis without a verified event-accounting
path. A held ticker/account valuation is explicitly provisional. This is a
known pilot limitation requiring a source-reviewed accounting enhancement,
not a reason to ignore the event. No action predating ownership should freeze
an unrelated new position.

Export the current year via stock-paper tax-export. Preserve acquisition and
sale dates, shares, FIFO basis including fees, proceeds, short/long holding
periods and raw realized gains/losses. Flag potential same-symbol replacement
buys within 30 days before/after losses, including January activity affecting
December losses. Preserve sell freedom; do not trap a losing position to avoid
a flag. These are unadjusted simulated records, not automatic wash-sale tax
calculations. Similar securities/outside accounts and special tax rules are
not covered. No fictional trade creates a real tax liability or filing entry.
Reference: https://www.irs.gov/publications/p550.

## Sources

Separate source observations from our executions. Save the original directional
claim, publication/observation date and transaction/quarter-end date when
applicable; preserve misses and corrections. Score sufficiently mature, unique
calls with matching windows, costs and uncertainty. Assess source quality by
verifiable evidence, independence and corrections, not fame/subscriber count.
13Fs may arrive 45 days after quarter-end; Congressional reports can arrive well
after the trade. They are slow research clues, not live order signals.
Reference: https://www.investor.gov/introduction-investing/investing-basics/glossary/form-13f-reports-filed-institutional-investment.
