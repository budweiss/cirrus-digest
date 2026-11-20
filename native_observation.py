"""S664: bounded read-only context before C1 native calls. No model loading."""
import json, math, subprocess, time, urllib.request

def before(model):
    out={}
    try:
        op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open('http://127.0.0.1:11434/api/ps',timeout=1) as response: rows=json.load(response)['models']
        matches=[r for r in rows if r.get('name')==model or r.get('model')==model]
        out['resident_before']=bool(matches)
        if matches:
            n=matches[0].get('context_length')
            if type(n) is int and n>0:out['resident_context_before']=n
    except Exception: pass
    try:
        p=subprocess.run(['systemctl','show','ollama.service','-p','ExecMainStartTimestampMonotonic','--value'],capture_output=True,text=True,timeout=1)
        start=int(p.stdout.strip())/1e6
        if p.returncode==0 and start>0:
            age=time.monotonic()-start
            if math.isfinite(age) and age>=0:out['runtime_uptime_seconds']=age
    except Exception: pass
    return out
