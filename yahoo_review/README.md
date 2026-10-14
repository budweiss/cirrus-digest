# Weekly Yahoo review on CUMULUS

Port of Buddy's existing header-only weekly workflow from the September 18
Codex workspace. The separate daily review application at Mac port 8769 is a
different workflow and is unchanged.

- `review_yahoo_locally.py` reads the newest 20 Inbox headers without a recorded
  choice. It never reads bodies or attachments or modifies the mailbox.
- `yahoo_review_lib.py` uses the existing CUMULUS Yahoo credential and only the
  C2 local model. Reports and state stay in `private/yahoo-review/`, mode 0700.
  Never print headers, subjects, senders, HTML or credentials to a cloud task.
- `yahoo_review_server.py` serves port 8638 on loopback only. A Mac SSH tunnel
  preserves the existing local URL. Choices require a current batch ID,
  same-origin JSON submission, exact message fingerprints and an explicit
  user selection. Decide later stays untracked. No unsubscribe is automatic.
- `apply_yahoo_actions.py` applies only due, explicitly submitted choices.
  Queue writers share a process lock. Fingerprints include Message-ID, sender,
  subject and date together. Migration found no existing queue or pending
  choices, so no legacy action fingerprints need conversion.

Runtime: CIRRUS dispatches Thursday 09:00 collection and daily 09:05 due actions.
CUMULUS1 hosts the private queue and loopback `yahoo-review-server.service`;
keeping that page beside its existing Yahoo credential and C2 inference is a
deliberate exception to the default CIRRUS placement for Buddy-facing work.
The Mac's `com.buddy.yahoo-review-tunnel` preserves the usual URL. Install its
`tunnel.py` as `~/Library/CoworkHelpers/yahoo_review_tunnel.py`: the launchd parent
loads existing Access credentials into the SSH child's environment, matching
the runner pattern. A bare SSH LaunchAgent cannot read the protected credential
file through its child proxy. Never put credentials into the plist or arguments.

October 14 cutover: 16 synthetic tests passed on both hosts, the browser
walkthrough retained a draft and left undecided rows available, and real
production launchd collection returned 20 headers, zero classification failures,
zero bodies and zero mailbox changes. The production apply worker found no due
choices and changed nothing. The legacy queue was empty before and after stopping
its server. Mac reports and rollback plists remain, the app heartbeat is paused,
and its completed chat is archived. The separate daily 8769 flow is unchanged.

Do not inspect real reports with cloud/browser tools. Use synthetic fixtures
for browser checks and local deterministic aggregate checks for real runs.
Before cutover, prove collection, the review page, queue preservation and the
scheduled entrypoints. Keep the old app schedule until those checks pass.
