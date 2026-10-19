"""Bounded owned-machine entry for desktop work. Prints metadata, never contents."""
import argparse
import fcntl
import json
from pathlib import Path
import answer_workflow as aw
from conversation_memory import Memory


def run(root, request_id, question, sources, public=False, models=None):
    memory=Memory(root)
    with (memory.directory/'desktop-worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        memory.recover('desktop')
        row=memory.add(owner='desktop',client='buddy',project='desktop',thread=request_id,
                       message_id=request_id,question=question,privacy='CLOUD_ALLOWED' if public else 'LOCAL_ONLY',
                       meta={'request_origin':request_id})
        if row['state']=='queued':
            creds=json.loads((Path(root)/'config/credentials.json').read_text()) if models is None else {}
            row=aw.compose(memory,row,models or aw.Models(root,creds),sources)
        memory.review_packet(row)
        return {'request_id':row['id'],'state':row['state'],
                'calls':row['meta'].get('attempt_budget',{}).get('calls',0),
                'local_result':str(memory.directory/(row['id']+'-review.json'))}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--request-id',required=True,help='Stable logical request ID; reuse on retry')
    p.add_argument('--question-file',required=True)
    p.add_argument('--sources-file',required=True,help='JSON object of original source texts')
    p.add_argument('--public',action='store_true',help='Explicitly attest question AND sources are non-private')
    args=p.parse_args()
    try:
        print(json.dumps(run(Path(__file__).resolve().parent,args.request_id,
                            Path(args.question_file).read_text(),json.loads(Path(args.sources_file).read_text()),args.public)))
    except Exception as exc:
        print(json.dumps({'state':'held','error_type':type(exc).__name__}))
        raise SystemExit(1)
