#!/bin/bash
# newsletter_fetch_scheduled.sh — what newsletter-fetch.service runs (S528).
#
# Both mailboxes, Yahoo then Gmail, each attempted even if the other fails, and
# ONE job_status record for the pair. One record per mailbox under the same key
# would let a Gmail success overwrite a Yahoo failure; that silent kind of gap
# is what left the archive empty from 2026-10-12 (T168).
cd /home/buddy/cirrus-digest || exit 1
rc=0
failed=""
.venv/bin/python newsletter_fetch.py || { rc=1; failed="yahoo"; }
NEWSLETTER_ACCOUNT_LABEL=gmail-buddy .venv/bin/python newsletter_fetch.py || { rc=1; failed="$failed gmail-buddy"; }
if [ "$rc" -eq 0 ]; then note="yahoo and gmail-buddy fetched"; else note="fetch failed:$failed"; fi
.venv/bin/python -c 'import sys, job_status; job_status.record("newsletterfetch", sys.argv[1] == "0", sys.argv[2])' "$rc" "$note"
exit "$rc"
