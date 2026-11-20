"""Read-only own-account cost aggregates; never export detail strings."""
import json, math, sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

def summary(day,path=Path('/opt/cumulus-supervisor/state/spend-ledger.jsonl')):
    start=datetime.strptime(day,'%Y-%m-%d').replace(tzinfo=ZoneInfo('America/New_York'));end=start+timedelta(days=1)
    n=0;costs=[];malformed=0
    try:
        with path.open() as f:
            for line in f:
                try:
                    row=json.loads(line);ts=datetime.fromisoformat(row['ts'])
                    if ts.tzinfo is None:ts=ts.replace(tzinfo=ZoneInfo('America/New_York'))
                    if not start<=ts<end:continue
                    n+=1;v=row.get('cost_usd')
                    if type(v) in (int,float) and math.isfinite(v) and v>=0:costs.append(v)
                except (ValueError,TypeError,KeyError):malformed+=1
        return dict(day=day,available=True,records=n,reported_cost_usd=round(sum(costs),6),unknown_cost_records=n-len(costs),malformed=malformed)
    except OSError:return dict(day=day,available=False)
if __name__=='__main__':print(json.dumps(summary(sys.argv[1])))
