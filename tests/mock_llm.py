"""A tiny OpenAI-compatible /chat/completions server for testing the LLM path offline.
Round 1 answers with an invalid plan (to exercise the repair loop), round 2 with the plan file given."""
import json, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
PLAN = open(sys.argv[1]).read() if len(sys.argv) > 1 else '{}'
calls = []
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        calls.append(dict(path=self.path, auth=self.headers.get('Authorization'), model=body['model'], n_messages=len(body['messages']), prompt_chars=sum(len(m['content']) for m in body['messages'])))
        content = '{"nc_plan_version":"0.1","title":"broken"}' if len(calls) == 1 else '```json\n' + PLAN + '\n```'
        out = json.dumps(dict(id='mock', object='chat.completion', model=body['model'] + '-mock', choices=[dict(index=0, message=dict(role='assistant', content=content), finish_reason='stop')])).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_GET(self):
        out = json.dumps(calls).encode(); self.send_response(200); self.send_header('Content-Length', str(len(out))); self.end_headers(); self.wfile.write(out)
HTTPServer(('127.0.0.1', int(sys.argv[2]) if len(sys.argv) > 2 else 8799), H).serve_forever()
