# Independent paper-account runbook

Active October 8, 2026. Read PROJECT.md and scorekeeping.md each run.
This replaces the personal-holdings workflow in the four existing tasks.

## Access and commands

From a mounted desktop-task workspace, use its actual path:
`COWORK=/sessions/<actual-session>/mnt/Cowork ./runner/submit.sh <unique-id> stock-paper '{"action":"refresh","slot":"am"}'`
The command and JSON args are separate arguments. Do not supply a whole
{cmd,args} object where the command name goes. Native Mac:
`python3 runner/stock_paper.py refresh --slot am`.

Actions: status, refresh, apply, research, research-config, tax-export.
Slot: am, pm, trial. File: simple JSON filename under
stock-pickers/private/paper-account/requests/, never an arbitrary path.
Use year=YYYY for tax-export. Read the returned exact report/result paths. Read order_outcomes as well as decisions: BUY intent alone is not a fill. The afternoon task may find a prepared launch draft from development; refresh the ledger and review it before sending.
A busy runner means wait/retry; a timeout means inspect the same request ID.
Never create a different order ID just to retry an uncertain execution.
If the new helper is unavailable, report the gap, keep trades pending and
do not fall back to personal holdings or invent balances.

## Every daily run

1. Read Buddy's email feedback through the existing cumulus-gmail-inbox helper
   and the preceding letter. No personal holdings/account alerts are inputs
   for stock selection. No old experiments imported into this account.
2. Run refresh, read canonical account, quote timestamps, decisions/trades.
   Inspect deliveries: if today's slot is already sent, do not trade or send
   again for that same scheduled run. If its receipt is uncertain, reconcile
   the existing sender/Sent-mail evidence before any resend; never remove a
   receipt to bypass the guard. The status action also returns these receipts.
   Check market state. Morning letters normally report plans, not executions.
3. Research positions and strong independent candidates using original SEC/IR
   evidence, price/valuation, competitors, news, earnings, buybacks, hiring and
   layoffs. Read full content, distinguish publication and observation dates.
   Corroborate company claims. Old research is context, not new evidence.
4. Keep the separate company manifest's held/watchlist roles aligned with the
   current paper ledger, using research-config after a position changes; retain
   independently verified identities and source seeds. Then optionally run
   research for the separate C1/C2 company notebook. Never
   launch it repeatedly when still running. Failed/unreviewed model drafts are
   gaps; direct source-reviewed research can still support a decision.
   A pass has a ten-minute analysis budget and can return an exact partial
   report. Read its unfinished-company list; partial is not complete coverage.
   Company order rotates to avoid repeatedly neglecting the same businesses.
5. Record BUY/SELL/HOLD decisions with rationale, valuation, counterargument,
   source URLs/publication dates, sell triggers, strategy version and fixed
   21/28-day review dates. Save request JSON privately and call apply. It fetches
   its own prices and enforces cash/shares. Read each actual fill/hold outcome.
6. Refresh after any trades. Include its exact report marker and canonical
   account section unchanged in digests/YYYY-MM-DD-am.md or ...-pm.md.
   Add narrative around it. Never recalculate ledger balances by hand.
7. Send through stock-digest-send with today's file and mode=live. The two
   daily emails are authorized. Sender verifies the ledger section and
   duplicate slot receipt. If outcome is unknown, inspect it rather than
   resend blindly. Finish with verified delivery status.

## Order input

Illustration only, not a stock recommendation:

```json
{
  "request_id": "unique-review-session",
  "decisions": [{
    "idempotency_key": "unique-idea-v1",
    "action": "BUY",
    "ticker": "EXAMPLE",
    "reason": "Business and price rationale",
    "strategy_version": "independent-v1",
    "evidence": [{"url": "https://company.example/release", "published": "2026-10-08", "claim": "Verified finding"}],
    "valuation": "Calculation and limitations",
    "counterargument": "Strongest objection",
    "exit_trigger": "What would justify a sale",
    "review_dates": ["2026-10-29", "2026-11-05"],
    "order": {"idempotency_key": "unique-order-v1", "budget": "10000.00", "limit_price": "100.00"}
  }]
}
```

BUY uses budget (whole shares calculated from adjusted fill) or explicit
integer shares; limit_price is the maximum acceptable adjusted price.
SELL uses owned integer shares; limit_price is the minimum acceptable price.
HOLD has no order. The CLI rejects caller-supplied fill prices.
IDs are immutable: same ID/same instructions returns the previous result;
same ID/different instructions is refused. A held order is not automatically
executed on a later run. Reconsider it in a new linked decision/version.

## Email order

- What we bought/sold, or why no trades occurred.
- Canonical cash/account/positions/trades, then pending ideas separately.
- Two or three connected explanations: company change, price/valuation,
  strongest counterargument, sell condition, dated source links.
- What earlier decisions taught us; next earnings/review dates; coverage gaps.
- Simulated realized/unrealized results and year-end-record location/limits.

Do not manufacture activity. Do not call an unfilled idea a position. Do not
present headline lists as the investment explanation. Keep wording plain.

## Saturday scorecard

Refresh, tax-export current year, read all decisions/trades and prior reviews.
Save a dated private review of ideas due at 21/28 days using original evidence,
and account performance versus recorded SPY reference over matching dates.
Show sample size, elapsed time, cash drag, costs and corporate-action gaps.
Classify mistakes: facts, valuation, timing, company or market development.
Change strategy versions only for subsequent decisions; retain losers/early
sales and fixed review dates. Save digests/scorecard-latest.md. No extra email.

## Source discovery and research configuration

Daily runs rotate company/SEC, competitor, public Berkshire/Fisher/13F,
House/Senate, newsletter and podcast/YouTube evidence. Save disclosure dates
separately from trades/quarter-end. Treat famous-investor activity as clues.

Sunday: discover 3–6 relevant sources, fetch actual original content, record
failures/usefulness, and trial accessible public sources without another
approval round. Save dated source history privately; generic registry in
sources.md. Use existing transcript tools/local models. No paid access,
subscriptions, contacting people or reactivating declined promotional mail.

research-config accepts the existing company manifest schema:
{"companies":[{"ticker":"EXAMPLE","cik":"123","name":"Verified company",
"role":"watchlist","hosts":["company.example"],"seeds":[
{"url":"https://company.example/investors","kind":"index"}]}]}.
Use independently verified identities/hosts; add careers/article seeds.
The previous universe is archived. Never configure from personal holdings.

## Honest limitations

Public last-trade quotes plus a 0.05% allowance are modeled fills, not broker executions.
Missing corporate actions cause a visible hold/provisional performance until
source-reviewed adjustments are available. Year-end exports are simulated
lot records, not filing-ready returns or automatic wash-sale calculations.
An immature/limited sample cannot prove stock-picking skill.
