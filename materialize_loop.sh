#!/bin/bash
# Self-healing loop wrapper for materialize_credentials.sh (S63). Re-runs the
# idempotent materialize script every 10s so a mid-uptime /dev/shm loss (cause
# still under investigation) self-corrects almost immediately instead of
# leaving up to a minute-long gap another job could hit.
#
# Interval tightened from 60s -> 10s (S63, same session, after CUMULUS intake
# and Skywarden both hit the gap during testing). Cost is negligible: age-decrypting
# a ~5KB file is sub-millisecond CPU, so running it 6x more often has no real
# overhead. This directly shrinks the vulnerability window regardless of what
# the actual root cause turns out to be -- unlike staggering job schedules,
# which wouldn't address it (the failure signature is a fully-missing file,
# not a partial-write race, and nothing else writes to this path to stagger
# away from).
#
# Previously only deployed directly to cumulus1 via scp, never committed --
# fixed here so it's git-tracked and backed up like everything else.
set -u
while true; do
    /home/buddy/cirrus-digest/materialize_credentials.sh || echo "materialize_credentials.sh failed (exit $?), will retry in 10s" >&2
    sleep 10
done
