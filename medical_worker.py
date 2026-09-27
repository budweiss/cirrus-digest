"""Bounded C2 medical evidence service, on the dedicated link only (S320).

No arbitrary model/options/system prompts, files, shell, or clinical decisions.
The worker owns admission and the full lease even if its HTTP client disconnects.
"""
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import alopecia_medical as medical
import local_specialists as specialists

ROOT = Path(__file__).resolve().parent
BIND = '192.168.100.11'
ALLOWED = {'192.168.100.10', '192.168.100.11'}
MAX_BODY = 40000


def validate_input(data):
    if not isinstance(data, dict) or set(data) != {'question', 'passages'}:
        raise ValueError('invalid_request')
    question, passages = data['question'], data['passages']
    if not isinstance(question, str) or not 1 <= len(question) <= 4000:
        raise ValueError('invalid_question')
    if not isinstance(passages, dict) or not 1 <= len(passages) <= 3:
        raise ValueError('invalid_passages')
    for key, value in passages.items():
        if key not in {'S1', 'S2', 'S3'} or not isinstance(value, dict) or set(value) != {'text'} or not isinstance(value['text'], str):
            raise ValueError('invalid_passage')
    if sum(len(v['text']) for v in passages.values()) > 16000:
        raise ValueError('evidence_context_too_large')
    return question, passages


def qwen_health():
    data = specialists.request('http://192.168.100.11:8000', '/v1/models')
    if not any(r.get('id') == 'qwen3.8-27b-fp8' for r in data.get('data', [])):
        raise specialists.Unavailable('protected_qwen_unavailable')


def health(root=ROOT):
    state = specialists.availability(root)['medical_evidence']
    state.update(host=socket.gethostname(), protocol=1)
    if (root/'logs/local-specialists/quarantined').exists():
        state.update(ready=False, reason='unload_requires_operator_check')
    qwen_health()
    return state


def extract(data, root=ROOT):
    question, passages = validate_input(data)
    state = health(root)
    if not state['ready']:
        raise specialists.Unavailable(state['reason'])
    try:
        raw = specialists.generate('medical_evidence', [
            {'role': 'system', 'content': medical.SYSTEM},
            {'role': 'user', 'content': json.dumps({'question': question, 'passages': passages})}
        ], root=root, creds={'llm_budget': {'box': 'cumulus2', 'ledger_path': 'logs/llm-spend-ledger.jsonl'}}, output_schema=medical.evidence_schema(passages))
    except specialists.Unavailable as exc:
        if str(exc) == 'specialist_unload_not_confirmed':
            (root/'logs/local-specialists/quarantined').write_text('Operator must verify backend idle before clearing.\n')
        raise
    qwen_health()
    data = medical.validate(raw, passages)
    return {'evidence': data, 'model': state['model'], 'host': 'cumulus2', 'unloaded': True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log medical questions/passages or request URLs.

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def reply(self, code, data):
        raw = json.dumps(data).encode()
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass  # Inference has already finished and released its lease.

    def permitted(self):
        if self.client_address[0] not in ALLOWED:
            self.reply(403, {'error': 'source_not_permitted'})
            return False
        return True

    def do_GET(self):
        if not self.permitted():
            return
        if self.path != '/health':
            return self.reply(404, {'error': 'unknown_endpoint'})
        try:
            self.reply(200, health())
        except Exception:
            self.reply(503, {'error': 'medical_worker_unavailable'})

    def do_POST(self):
        if not self.permitted():
            return
        if self.path != '/evidence':
            return self.reply(404, {'error': 'unknown_endpoint'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= MAX_BODY or self.headers.get('Transfer-Encoding'):
                raise ValueError('invalid_size')
            data = json.loads(self.rfile.read(size))
            validate_input(data)
        except (ValueError, TimeoutError):
            return self.reply(400, {'error': 'invalid_request'})
        try:
            self.reply(200, extract(data))
        except specialists.Unavailable:
            self.reply(503, {'error': 'specialist_unavailable'})
        except ValueError as exc:
            known = {'invalid_evidence_schema', 'inconsistent_abstention', 'unsupported_quote'}
            reason = str(exc) if str(exc) in known else 'invalid_evidence_output'
            self.reply(422, {'error': 'evidence_rejected', 'reason': reason})
        except Exception:
            self.reply(503, {'error': 'medical_evidence_failed'})


if __name__ == '__main__':
    if socket.gethostname() != 'cumulus2':
        raise SystemExit('C2-only worker')
    with ThreadingHTTPServer((BIND, 8012), Handler) as server:
        server.daemon_threads = False  # Shutdown waits for inference/unload, not client lifetime.
        server.serve_forever()
