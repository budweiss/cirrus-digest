"""Small read-only health contract shared by Skywarden and CIRRUS watchdog."""
import json
import time
from pathlib import Path

PATH=Path('/home/buddy/model-serving/fleet/state-v2/status.json')


def check(path=PATH,now=None):
    now=time.time() if now is None else now
    try:
        data=json.loads(Path(path).read_text())
        if not 0 <= now-data['observed'] <= 180:
            return {'ok':False,'reason':'fleet controller heartbeat stale'}
        # S497: pass through what the controller already reports (fresh data only), so
        # the CIRRUS watchdog can name a stuck job and warn before a qualification lapses.
        # The ok/reason verdict that Skywarden shares is unchanged.
        extra={'unsettled':data.get('unsettled') or [],'qualifications':data.get('qualifications') or []}
        if not isinstance(data['workers'], list) or len(data['workers'])!=2 or data.get('ok') is not True:
            return dict(extra,ok=False,reason='fleet controller reports degraded resources or unresolved pilot')
        return dict(extra,ok=True,reason='both workers observed; production recovery remains with Skywarden')
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        return {'ok':False,'reason':'fleet controller status missing or unreadable'}

if __name__=='__main__':print(json.dumps(check()))
