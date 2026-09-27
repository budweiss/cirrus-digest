"""Read-only C2 resource facts on the dedicated link. No secrets or command API.

C1 cannot SSH to C2 using its current identity. This bounded host adapter
publishes only memory/boot/uptime metadata, with no shell or model mutations.
"""
import json
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BIND='192.168.100.11'
ALLOWED={'192.168.100.10','192.168.100.11'}


def facts(proc=Path('/proc')):
    values={line.split(':')[0]:int(line.split()[1]) for line in (proc/'meminfo').read_text().splitlines()}
    return {'host':socket.gethostname(),'observed':time.time(),
            'available_mib':values['MemAvailable']//1024,
            'swap_used_mib':(values['SwapTotal']-values['SwapFree'])//1024,
            'boot_id':(proc/'sys/kernel/random/boot_id').read_text().strip(),
            'uptime_seconds':float((proc/'uptime').read_text().split()[0])}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        status=200
        if self.client_address[0] not in ALLOWED:
            status,body=403,{'error':'source_not_permitted'}
        elif self.path!='/health':
            status,body=404,{'error':'unknown_endpoint'}
        else:
            try:body=facts()
            except Exception:status,body=503,{'error':'resource_state_unreadable'}
        raw=json.dumps(body).encode()
        self.send_response(status);self.send_header('Content-Type','application/json')
        self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)))
        self.end_headers();self.wfile.write(raw)

if __name__=='__main__':
    if socket.gethostname()!='cumulus2':raise SystemExit('C2-only adapter')
    ThreadingHTTPServer((BIND,8011),Handler).serve_forever()
