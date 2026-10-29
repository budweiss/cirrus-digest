# Stock Pickers — independent paper portfolio

**Current mandate: Buddy, October 8, 2026 (S386).** Start a clean fictional
$200,000 account and choose our own stocks. Buy/sell within that account,
explain each move, retain its evidence, and report positions, cash and gains/
losses twice daily. This supersedes the Robinhood-centered charter and old
$1,000 experiments. Historical files remain intact, excluded from the new
account. Never read or import Buddy's holdings to select stocks.

Read **PAPER-RUNBOOK.md** for the daily workflow and **scorekeeping.md** for
accounting and learning. All four existing scheduled tasks use this mandate.

## Account and price rules

- One account, independent-200k-v1, one $200,000 initial deposit, no topups/reset.
- US-listed USD common stocks, whole shares, long only. No borrowing, leverage,
  shorting, options or actual brokerage orders. Cash earns zero in this pilot.
- Buy only with available settled cash; sell only owned shares. Profits can
  be reinvested and losses reduce buying power. Never create extra capital.
- Fresh provider-stamped prices at trade time; no backdated opening-price
  fills or prices remembered from a previous letter. Regular market only.
- Public last-trade quotes are paper reference prices, not guaranteed exchange
  executions. Reject stale, future, explicitly delayed or missing quotes.
  Preserve an unknown provider-delay label rather than claim licensed realtime.
- Include a 0.05% adverse allowance in each fill. Save source, quote timestamp,
  adjusted fill, shares and rationale. Keep plans separate from executed trades.
- No need to spend all cash or trade twice daily. Explain holding cash/positions.
- Business quality, valuation and varied company exposure guide initial picks.
  New evidence can justify any paper sale; legacy dividend-portfolio restrictions
  do not apply. The account does not inherit Buddy's personal risk/position mix.

## Required email

Start with what we BOUGHT/SOLD and why, or "No trades this run" with reasons.
Show canonical ledger totals and current positions: cash, account value,
change from $200,000, realized and unrealized gains/losses. Distinguish pending
ideas. Explain consequential decisions as connected company stories: what
changed, why this business at this price, counterarguments and sell conditions.
Use dated sources. A headline inventory is supporting material, not the report.
Show actual quote times; incomplete valuations remain incomplete, not cost-filled.

## Runtime

CUMULUS1 owns private/stock-paper/ under /home/buddy/cirrus-digest: SQLite
ledger, immutable decisions/trades, quote evidence and account reports.
runner/stock_paper.py is the Mac bridge; scheduled tasks use the stock-paper
runner command. Private mirrors/requests are in stock-pickers/private/paper-account/.
C1's existing backup covers this folder. CUMULUS2's existing local Qwen model
helps research; it does not control money calculations.

Separate company notebook: private/stock-paper/research/ on C1. Reuse SEC/
company collection, original-source archives and the shared model lock.
Only our independent universe belongs here. Drafts need source review.
CIRRUS remains development/oversight. No new model download, broker API,
paid data subscription or duplicate sender is required.

## Research expansion

Use accessible SEC filings, company IR/earnings/call transcripts, competitors,
customers, valuation, buybacks, hiring/layoffs, newsletters, podcasts, YouTube
transcripts and useful public websites. Track unique job postings and dates;
a job ad alone does not prove growth, revenue or a new factory.

Follow public Congressional disclosures and Berkshire/Fisher holdings.
Save both transaction/quarter-end date and public disclosure date. These lagged
clues do not prove anyone is buying now. Verify the company independently.
Fisher's public research is now in scope; its previously declined promotional
email subscription stays declined. Trial useful public sources automatically,
retaining identity, actual coverage, conflicts, failures and usefulness.
No paywall bypass, new paid access, promotional subscriptions or outreach.
"Company sources" means public publications, not messages to people.

## Newsletter evidence coverage (S537)

Read approved general financial newsletters from both archived inbox feeds,
not only Seeking Alpha. Use the actual email date for freshness; it is not an
article publication date. Scan saved bodies for relevant passages and select
a bounded, publisher-diverse packet. Personal holdings/followed-ticker alerts,
unknown senders, stale or undated issues and duplicate copies are excluded.
Record screened, selected, excluded and deferred coverage; selected excerpts
are not full articles. Evaluate conflicting views without treating volume as
independent confirmation. Newsletter contributions require exact excerpt quotes
and corroborating dated primary evidence, plus the existing independent review.
Do not force a trade, introduce a new company without accepted research, or
claim to have fetched linked websites merely because an email mentions them.

## Existing schedule and delivery

America/New_York, with the app's normal several-minute run delay:

- Weekdays 07:45: morning recap/plans; regular market is still closed.
- Weekdays 14:45: afternoon review, eligible paper trades and recap.
- Saturday 09:00: private account/decision/source scorecard.
- Sunday 09:00: discovery and bounded trials of public sources.

Skip NYSE holidays; after an early close recap without fills. Do not create
duplicate tasks. Scheduling remains desktop-owned: Mac awake/online, Claude
running. The ledger is server-owned; scheduling has not silently moved there.
Buddy explicitly authorized the revised twice-daily emails October 8. Use
stock-digest-send to his established Yahoo inbox, no other recipients.

## Records, taxes and learning

Preserve original decisions, revisions, fills, lots, proceeds, basis and fees;
separate realized/unrealized gains and short/long holding periods. Export
year-end simulated records with possible wash-sale/corporate-action flags.
These are paper records, not a real tax return or broker form. No actual
tax liability arises from fictional trades. Never merge with Robinhood.
Missing adjustments stay visible; never claim tax/total-return completeness
when the necessary corporate-action evidence is absent.

Review original ideas at 21/28 calendar days (next trading day when needed)
and track the managed account as it changes. Preserve losers and early sales.
Compare against recorded SPY evidence, disclose costs/cash/adjustment gaps,
and test changed rules prospectively. A few winners do not establish skill.

## Privacy

Picks, quantities, prices, balances and reports belong only in gitignored
private/ or digests/, on Buddy's existing machines and in emails to him.
Only code and generic instructions go to Git. Personal holdings, legacy calls,
old experiments and dated corrections remain preserved as separate history.
No Robinhood credentials, API, real transactions or automatic go-live.
