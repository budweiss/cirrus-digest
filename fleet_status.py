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
        if len(data['workers'])!=2 or not data.get('ok'):
            return {'ok':False,'reason':'fleet controller reports degraded resources or unresolved pilot'}
        return {'ok':True,'reason':'both workers observed; production recovery remains with Skywarden'}
    except (OSError,ValueError,KeyError,TypeError):
        return {'ok':False,'reason':'fleet controller status missing or unreadable'}

if __name__=='__main__':print(json.dumps(check()))
