"""One bounded synthetic repair comparison. No production data or deployment.

Run explicitly on CIRRUS. Four requests, one optional truncated-answer retry,
durable $2 total reservation.
This is a small smoke evaluation, not a production accuracy leaderboard.
"""
import ast
import json
from pathlib import Path
import re
import sys
import tempfile
import time

import llm_budget as B
import llm_providers as L
import runtime_isolation

ROOT = Path(__file__).resolve().parent
RUN = ROOT / 'logs/telewarden/model-eval-s614.json'
ROUTES = [
    ('anthropic','claude-sonnet-5-5','claude_dev_model',2,10),
    ('anthropic','claude-opus-5-5','claude_dev_model',4,20),
    ('openai','gpt-6-astra','openai_model',10,50),
    ('deepseek','deepseek-flash','deepseek_model',0.3,1.2),
]
PROMPT = '''Return only JSON {"code":"Python source"}. Implement these four pure functions.
No imports, file access, network, or top-level execution. Preserve signatures.
1. classify(completed, gates_ok, sent, expired): return "failed" if not completed,
not sent, or expired. Otherwise return "healthy" if gates_ok else "review_needed".
A rejected review must not be called healthy, but is not a process crash.
2. should_retry(last_epoch, due, running, attempted): return False when running,
already attempted this occurrence, or last_epoch is not None and >= due.
Otherwise True. A completed failed attempt still must not be repeated.
3. failed_count(results): count values that are exactly False or None.
True means delivered. Other values must raise ValueError, including integers.
4. can_claim(status, owner, lease_owner, lease_until, now): status must be one
of new/explained/regressed. An unexpired lease held by a different nonempty
owner blocks claiming. A missing owner or expired lease does not block.
Return bool. No need to change any state.'''
TESTS = {
 'review_status': '''from solution import classify
assert classify(True,False,True,False)=='review_needed'
assert classify(True,True,True,False)=='healthy'
for x in [(False,True,True,False),(True,True,False,False),(True,False,True,True)]:
 assert classify(*x)=='failed'
''',
 'no_duplicate_retry': '''from solution import should_retry
assert not should_retry(100,100,False,False)
assert not should_retry(101,100,False,False)
assert should_retry(99,100,False,False)
assert should_retry(None,100,False,False)
assert not should_retry(None,100,True,False)
assert not should_retry(None,100,False,True)
''',
 'delivery_count': '''from solution import failed_count
assert failed_count([True,False,None])==2
assert failed_count([])==0
for v in [0,1,'',[],{}]:
 try: failed_count([v])
 except ValueError: pass
 else: raise AssertionError('invalid delivery value accepted')
''',
 'case_ownership': '''from solution import can_claim
assert can_claim('new','a','b',100,100)
assert not can_claim('new','a','b',101,100)
assert can_claim('regressed','a','a',101,100)
assert can_claim('explained','a',None,101,100)
assert not can_claim('fixing','a','a',101,100)
assert not can_claim('fixed','a',None,0,100)
''',
}


def main():
    RUN.parent.mkdir(parents=True,exist_ok=True)
    # Persist reservations before calls; rerunning resumes, never resets spend.
    state=json.loads(RUN.read_text()) if RUN.exists() else {'reserved_usd':0,'results':[]}
    creds=json.loads((ROOT/'config/credentials.json').read_text())
    creds['llm_privacy']=creds.get('llm_privacy') or 'CLOUD_ALLOWED'
    cfg,box,ledger=B.resolve(creds,app_dir=str(ROOT))
    if not cfg or not ledger: raise RuntimeError('budget unavailable')
    for provider,name,key,ip,op in ROUTES:
        previous=[r for r in state['results'] if r['requested']==name]
        for r in previous:
            u=r.get('usage',{})
            if u.get('input') is not None and u.get('output') is not None:
                r['cost_upper_usd']=(u['input']*ip+u['output']*op)/1000000
        retry=('--retry-truncated' in sys.argv and len(previous)==1
               and previous[0].get('finish')=='length')
        if previous and not retry:continue
        tokens=8192 if retry else 2500
        reserve=(len(PROMPT.encode())*ip+tokens*op)/1000000*1.25
        allowed,_=B.allow('telewarden-model-eval',reserve,cfg,box=box,ledger_path=ledger)
        if state['reserved_usd']+reserve>2 or not allowed:break
        row={'requested':name,'status':'reserved','reserved_usd':reserve,'attempt':len(previous)+1}
        state['results'].append(row);state['reserved_usd']+=reserve
        RUN.write_text(json.dumps(state,indent=2))
        c=dict(creds);c[key]=name
        started=time.monotonic()
        try:
            reply=L.call(provider,'Return exact JSON and correct pure Python.',PROMPT,c,
                         max_tokens=tokens,retries=0,task='telewarden-model-eval',
                         privacy='CLOUD_ALLOWED',strict_accounting=True)
            row.update(actual=L.last_model(),finish=L.last_finish_reason(),
                       usage=getattr(L._LAST,'usage',{}))
            u=row['usage']
            row['cost_upper_usd']=(u['input']*ip+u['output']*op)/1000000
            if not row['actual']: raise ValueError('response identity missing')
            code=json.loads(re.sub(r'^```(?:json)?\s*|\s*```$','',reply.strip()))['code']
            tree=ast.parse(code)
            if (any(not isinstance(n,ast.FunctionDef) for n in tree.body)
                    or any(isinstance(n,(ast.Import,ast.ImportFrom,ast.Attribute)) for n in ast.walk(tree))
                    or any(isinstance(n,ast.Name) and n.id.startswith('__') for n in ast.walk(tree))
                    or any(isinstance(n,ast.Call) and (not isinstance(n.func,ast.Name) or
                        n.func.id not in {'sum','isinstance','type','ValueError','any','all','bool','len'})
                        for n in ast.walk(tree))):
                raise ValueError('non-pure solution')
            passed=[]
            with tempfile.TemporaryDirectory(prefix='telewarden-model-eval-') as td:
                p=Path(td);(p/'solution.py').write_text(code)
                for label,test in TESTS.items():
                    (p/'check.py').write_text(test)
                    r=runtime_isolation.run([sys.executable,'check.py'],p,timeout=15)
                    if r.returncode==0:passed.append(label)
            u=row['usage']
            row.update(status='tested',passed=passed,score=len(passed),total=len(TESTS),
                       cost_upper_usd=(u['input']*ip+u['output']*op)/1000000)
        except Exception as e:row.update(status='failed',reason=type(e).__name__)
        row['seconds']=round(time.monotonic()-started,2)
        RUN.write_text(json.dumps(state,indent=2))
    RUN.write_text(json.dumps(state,indent=2))
    print(json.dumps(state))


if __name__=='__main__':main()
