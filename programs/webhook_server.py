#!/usr/bin/env python3
"""Tiny HTTP server that prints alerts POSTed by taskmaster (http_alert bonus).
usage: python3 programs/webhook_server.py [port]   (default 8080)"""
import http.server, json, sys

class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        print("ALERT RECEIVED:", json.dumps(json.loads(body), indent=2), flush=True)
        self.send_response(200); self.end_headers()
    def log_message(self, *a):
        pass

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
print(f"listening on http://127.0.0.1:{port}", flush=True)
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
