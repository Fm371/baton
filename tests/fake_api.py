"""A tiny backend for real-data-smoke tests. Usage: fake_api.py PORT

The environment variable FAKE_DB stands for the database URL baton hands the
backend. A URL containing "nokey" plays a real database whose resource type is
named differently from the one the code hardcodes.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        self.reply(200, {"db": os.environ.get("FAKE_DB", ""),
                         "env": {k: v for k, v in os.environ.items() if k.startswith("FAKE_")}})

    def do_POST(self):
        if self.path == "/auth/login":
            ok = self.body().get("password") == "pw-from-env"
            return self.reply(200 if ok else 401, {"access_token": "tok-123"} if ok else {"detail": "bad login"})
        if self.path == "/plans/p1/enroll":
            if self.headers.get("Authorization") != "Bearer tok-123":
                return self.reply(401, {"detail": "no token"})
            if "nokey" in os.environ.get("FAKE_DB", ""):
                return self.reply(400, {"detail": "resource type 'person' not found"})
            return self.reply(200, {"status": "enrolled"})
        self.reply(404, {"detail": "no such route"})

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
