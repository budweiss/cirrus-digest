"""Owned-host diagnostic: immutable one-attempt receipt, private transport capture."""
import argparse, io, json, os, pathlib, sys, time, urllib.request

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--sources',required=True);p.add_argument('--out',required=True);p.add_argument('--case',type=int,required=True);p.add_argument('--reviewer',type=int,choices=[0,1],required=True);a=p.parse_args()
    os.umask(0o077);out=pathlib.Path(a.out);out.mkdir(parents=True,exist_ok=True)
    marker=out/'attempt.json'
    try:
        with marker.open('x') as f:json.dump({'started_at':time.time(),'case':a.case,'reviewer':a.reviewer},f)
    except FileExistsError:
        print(json.dumps({'state':'already_attempted'}));return
    sys.path.insert(0,a.root);import foundation_renewal_auto as A
    sources=json.loads(pathlib.Path(a.sources).read_text());case=sources['case_'+str(a.case)]
    original=urllib.request.build_opener;responses=[]
    class Opener:
        def __init__(self,*args,**kw):self.inner=original(*args,**kw)
        def open(self,req,*args,**kw):
            with self.inner.open(req,*args,**kw) as r:raw=r.read()
            responses.append(json.loads(raw));(out/'transport.json').write_text(json.dumps(responses));return io.BytesIO(raw)
    urllib.request.build_opener=Opener
    result={'state':'error'}
    try:
        v=A.model_review(A.REVIEW_PROMPTS[a.reviewer],case,local=True)
        (out/'review.json').write_text(json.dumps(v));result={'state':'valid','verdict':v['verdict']}
    except Exception as e:
        (out/'exception.json').write_text(json.dumps({'type':type(e).__name__,'message':str(e)}))
        known=['local reviewer returned no rationale','local review failure is not anchored in NEW','non-failing local review must not cite evidence','renewal GPT-OSS identity, completion or context mismatch','renewal Qwen identity, completion or context mismatch','renewal Qwen input exceeds context budget','local reviewer returned no valid verdict','local reviewer returned no valid issue type']
        result['error']=str(e) if str(e) in known else type(e).__name__
    result['transport_responses']=len(responses)
    if responses:
        raw=responses[-1];result['model']=raw.get('model') if raw.get('model') in (A.MODEL,A.SECOND_MODEL) else 'unexpected_model';result['finish']=raw.get('done_reason') or (raw.get('choices') or [{}])[0].get('finish_reason')
        text=raw.get('message',{}).get('content') or (raw.get('choices') or [{}])[0].get('message',{}).get('content','')
        try:
            d=json.loads(text);result['schema_keys_match']=set(d)==set(A.REVIEW_SCHEMA['required']);result['reason_chars']=len(d.get('reason',''));result['reason_has_newline']='\n' in d.get('reason','')
        except Exception:result['json_valid']=False
    (out/'summary.json').write_text(json.dumps(result));print(json.dumps(result))
if __name__=='__main__':main()
