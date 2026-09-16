#!/usr/bin/env python3
"""Import existing SDK transcript costs once, preserving timestamps.

No model calls. Amounts are rounded transcript totals, explicitly labelled.
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import llm_budget


def main():
    creds = json.loads((ROOT / 'config/credentials.json').read_text())
    count = 0
    for path in sorted((ROOT / 'logs/alopecia-agent/transcripts').glob('*.md')):
        match = re.fullmatch(r'(?:run|dryrun)-(\d{8}-\d{6})(?:-(\d{6}))?\.md', path.name)
        if not match:
            continue
        cost = re.search(r'^Cost: \$(\d+(?:\.\d+)?)\s*$', path.read_text(), re.M)
        if not cost:
            raise ValueError('transcript lacks SDK cost: ' + path.name)
        stamp = match[1] + ('-' + match[2] if match[2] else '')
        date = datetime.strptime(match[1], '%Y%m%d-%H%M%S').replace(tzinfo=ZoneInfo('America/New_York'))
        llm_budget.record_sdk_cost(creds, float(cost[1]), task='alopecia-agent:coordinator',
                                  run_id='alopecia-sdk:' + stamp, app_dir=str(ROOT),
                                  ts=date.isoformat(), cost_basis='sdk_transcript_rounded')
        count += 1
    print(f'checked {count} SDK transcripts; idempotent import complete')


if __name__ == '__main__':
    main()
