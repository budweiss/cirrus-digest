# How Claude Should Review CIRRUS Proposals

Context: `digests/proposals/proposal-*.md` files are drafted automatically
by qwen2.5:72b (a 72B local model with no codebase access — only the
`PROJECT_CONTEXT` description, now extended with `CIRRUS-CONVENTIONS.md`).
Each has a "Review Checklist" with four boxes: Reviewed by Buddy, Reviewed
by Claude (Cowork), Implemented and deployed, Rejected (not a good fit).

At the start of a session where proposals are pending review (check via
`/proposals` in Telegram or the `digests/proposals/` folder), Claude should:

## 1. Read each proposal against `CIRRUS-CONVENTIONS.md`

Check the proposed file(s), framework/library choices, and scheduling
approach against the ground truth in that file. A proposal that contradicts
it (wrong bot framework, retraining Ollama, cron/`schedule` loops, etc.) is
**not implementable as written**, regardless of how reasonable its premise
sounds.

## 2. Classify each proposal into one of three buckets

- **Implementable as-is**: file/function references are real, the approach
  fits the conventions doc, the diff would be small. → Offer to implement
  it directly this session.
- **Good idea, bad implementation**: the underlying recommendation is sound
  but the code sketch is generic/wrong. → Don't implement the sketch. Draft
  a fresh, scoped proposal yourself (Claude has real file access; qwen
  doesn't) and note the original as superseded.
- **Not a good fit**: vague, out of scope, or contradicts how CIRRUS works
  and there's no salvageable kernel. → Mark rejected.

## 3. Update the checklist, don't just discuss

For each reviewed proposal, edit its file to:
- Check `[x] Reviewed by Claude (Cowork)`
- Either check `[x] Implemented and deployed` (after actually deploying),
  check `[x] Rejected (not a good fit)`, or leave both unchecked with a
  one-line note like `**Claude note:** generic/not implementable as written;
  kernel idea — <theme> — captured in proposal-YYYY-MM-DD-N.md instead.`

Do this via direct file edits (scp'd or edited in this Cowork mirror then
pushed), not by asking Buddy to do it manually — that's the point of having
Claude in the loop.

## 4. When drafting a *replacement* proposal

Use the same `# Proposal: ...` / `## Analysis` / `## Proposed Change` /
`## Risks / Things to Verify` / Review Checklist format as
`generate_proposal()` produces, so it's consistent and trackable. Reference
real line numbers / function names from the actual files. Pre-check
`Reviewed by Claude (Cowork)` since Claude wrote it.

## 5. Don't re-litigate settled decisions

If a proposal's theme was already discussed and explicitly deprioritized in
a past session (e.g. "dynamic scheduling instead of fixed 7am — not needed"),
say so briefly and mark rejected rather than re-opening the debate.
