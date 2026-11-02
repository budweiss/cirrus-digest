"""Prepare supported text requests locally, through the qualified answer workflow.

No mail send function is imported or called. Arbitrary files, fresh web research,
unknown effects and exhausted requests remain explicit blockers, never fake work.
"""
import fcntl
import json
import re
from pathlib import Path
import intake_lifecycle as life


def supported(question):
    # Only text transformations already served by the bounded composition path.
    if re.search(r'(?i)\b(excel|spreadsheet|workbook|pdf|docx|powerpoint|website|search|find|latest|current|video|rumble|stocks?|portfolio|holdings?)\b',question):
        return False
    return bool(re.search(r'(?i)\b(draft|rewrite|revise|summari[sz]e|combine|reword|write)\b',question))


def prepare(root, row, rec, creds, models=None, collect=None):
    import answer_workflow as aw
    from conversation_memory import Memory
    import task_solver
    question=task_solver.strip_quoted_reply(row[3])
    if not supported(question):
        life.update(root,row,'blocked_no_worker','cowork',
                    'request needs a research/file/tool worker; reconcile full mail and assign')
        return 'blocked_no_worker'
    memory=Memory(root)
    # Reuse the original intake identity and spent attempt ledger.
    turn=aw.intake_record(root,rec,row[2],'LOCAL_ONLY')
    if turn['state'] in ('running','sending','delivery_unknown','delivered','needs_review'):
        life.update(root,row,'blocked_on_evidence','cowork',
                    'existing attempt or delivery needs review; never reset or replay',
                    {'answer_request_id':turn['id']})
        return 'blocked_on_evidence'
    with (memory.directory/'intake-answer.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'busy'
        # INSERT OR IGNORE preserves existing intake privacy. Tighten that
        # SAME identity explicitly; never create a new budget to get local-only.
        turn=memory.update(turn['id'],privacy='LOCAL_ONLY')
        if turn['state'] != 'ready':
            if not memory.transition(turn['id'], turn['state'], 'running'):
                return 'busy'
            turn=memory.get(turn['id'])
        life.update(root,row,'working','client_request_prepare','compose and verify local text draft')
        try:
            if collect is None:
                import workflow_mail_sources
                collect=workflow_mail_sources.collect
            sources=collect(root,rec,creds)
            turn=aw.compose(memory,turn,models or aw.Models(root,creds),sources)
            artifact=memory.review_packet(turn)
            if turn['state']=='ready':
                life.update(root,row,'ready_for_send','cowork',
                    'review saved draft and obtain specific send approval',
                    {'artifact':str(artifact),'answer_request_id':turn['id']})
                return 'ready_for_send'
            life.update(root,row,'blocked_on_evidence','cowork',
                'local preparation did not pass; inspect saved evidence and preserve budget',
                {'artifact':str(artifact),'answer_request_id':turn['id']})
        except Exception as exc:
            aw.hold(memory,turn,'Preparation evidence unavailable: '+type(exc).__name__,requires_buddy=True)
            life.update(root,row,'blocked_on_evidence','cowork',
                'preparation failed: '+type(exc).__name__+'; inspect original evidence',
                {'answer_request_id':turn['id']})
        return 'blocked_on_evidence'


def run(root):
    import intake
    directory=Path(root)/'data/intake-lifecycle'
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (directory/'prepare.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:return
        with life.connect(root) as db:
            # A dead worker's in-flight model calls are uncertain, not free retries.
            db.execute("UPDATE requests SET state='blocked_on_evidence',owner='cowork',"
                       "next_action='preparation interrupted; inspect saved attempt without reset' "
                       "WHERE owner='client_request_prepare' AND state='working'")
            rows=[json.loads(r['payload']) for r in db.execute(
                "SELECT payload FROM requests WHERE state='queued_preparation' ORDER BY created LIMIT 1")]
        allowlist=intake.load_allowlist()
        creds=intake.load_json(intake.CREDS_PATH) or {}
        for row in rows:
            entry=allowlist.get(row[1])
            if not entry:
                life.update(root,row,'blocked_on_evidence','cowork','sender policy changed; review locally');continue
            rec={'requester':entry['name'],'projects':entry['projects'],'message_id':row[4],
                 'from':row[1],'from_email':row[1],'title':row[2],
                 'conversation_body':row[3],'body_head':row[3],'kind':'preparation'}
            prepare(root,row,rec,creds)



if __name__=='__main__':
    run(Path(__file__).resolve().parent)
