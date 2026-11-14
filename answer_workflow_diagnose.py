"""Owned-host diagnostic wrapper; unchanged admission, privacy and attempt limits."""
import argparse
import json
import os
import uuid
import time
from pathlib import Path
import answer_workflow as aw
import answer_workflow_cli as cli
from conversation_memory import Memory


def preflight(root, request_id, question, sources):
    memory = Memory(root)
    row = dict(owner='desktop', client='buddy', project='desktop', thread=request_id,
               question=question, privacy='LOCAL_ONLY', id='preflight', created=time.time())
    _, context, corrections, _ = aw.history_context(memory, row)
    sources = {**sources, **corrections, 'user-provided-request': question}
    payload = dict(question=question, sources={k:aw.canonical_view(v) for k,v in sources.items()},
                   history=context, mode='draft', drafts=[])
    size = len(json.dumps(payload, ensure_ascii=False).encode())
    limit = json.loads((Path(root)/'config/answer-workflow-capabilities.json').read_text())['max_user_bytes']
    return {'input_bytes':size, 'max_input_bytes':limit, 'fits':size <= limit}


def response_metadata(result, payload):
    """Content-free reasons for an unchanged parser's rejection."""
    raw = result.get('raw', '')
    parsed = aw.parse(raw, payload['sources'])
    summary = {'accepted_shape': parsed is not None}
    try:
        value = json.loads(raw)
        summary['json_object'] = isinstance(value, dict)
        if not isinstance(value, dict):
            return summary
        answer = value.get('answer')
        if isinstance(answer, str):
            summary['answer_chars'] = len(answer.strip())
            summary['passing_answer_too_short'] = value.get('verdict') == 'pass' and len(answer.strip()) < 20
        evidence = value.get('evidence')
        if isinstance(evidence, list):
            summary['invalid_quote_count'] = sum(
                not isinstance(item, dict) or not isinstance(item.get('quote'), str)
                or len(item['quote'].strip()) < 4
                or item['quote'] not in payload['sources'].get(item.get('source'), '')
                for item in evidence)
    except (ValueError, TypeError, AttributeError):
        summary['json_object'] = False
    return summary


class RecordedModels:
    def __init__(self, delegate, directory):
        self.delegate, self.directory = delegate, Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.receipts = []

    def call(self, payload, **kwargs):
        path = self.directory/(uuid.uuid4().hex+'.json')
        # Create the private receipt before inference. Never log credentials.
        fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        record = {'payload':payload, 'privacy':kwargs.get('privacy'), 'state':'attempted'}
        with os.fdopen(fd, 'w') as f:json.dump(record, f)
        self.receipts.append(path)
        try:
            result = self.delegate.call(payload, **kwargs)
            record.update(state='returned', result=result, metadata=response_metadata(result, payload))
            path.write_text(json.dumps(record))
            return result
        except Exception as exc:
            record.update(state='error', error=aw.stage_error(exc))
            path.write_text(json.dumps(record))
            raise


def run(root, request_id, question, sources):
    root = Path(root)
    measured = preflight(root, request_id, question, sources)
    if not measured['fits']:
        return dict(state='input_too_large', **measured)
    creds = json.loads((root/'config/credentials.json').read_text())
    recorder = RecordedModels(aw.Models(root, creds), root/'data/conversations/diagnostic-receipts')
    result = cli.run(root, request_id, question, sources, public=False, models=recorder)
    result['private_receipt_count'] = len(recorder.receipts)
    result['response_checks'] = [json.loads(p.read_text()).get('metadata', {}) for p in recorder.receipts]
    return result


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',default=str(Path(__file__).resolve().parent))
    p.add_argument('--request-id',required=True)
    p.add_argument('--question-file',required=True)
    p.add_argument('--sources-file',required=True)
    a=p.parse_args()
    try:
        print(json.dumps(run(a.root,a.request_id,Path(a.question_file).read_text(),json.loads(Path(a.sources_file).read_text()))))
    except Exception as exc:
        print(json.dumps({'state':'held','error_type':type(exc).__name__}))
        raise SystemExit(1)
