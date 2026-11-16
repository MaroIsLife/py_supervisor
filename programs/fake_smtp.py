#!/usr/bin/env python3
"""Minimal SMTP server that prints received e-mails (email_alert bonus).
usage: python3 programs/fake_smtp.py [port]   (default 1025)"""
import socketserver, sys

class H(socketserver.StreamRequestHandler):
    def say(self, s):
        self.wfile.write((s + "\r\n").encode())
    def handle(self):
        self.say("220 fake-smtp ready")
        data = False
        lines = []
        for raw in self.rfile:
            line = raw.decode(errors="replace").rstrip("\r\n")
            if data:
                if line == ".":
                    data = False
                    print("=== MAIL RECEIVED ===\n" + "\n".join(lines) + "\n=====================", flush=True)
                    lines = []
                    self.say("250 OK")
                else:
                    lines.append(line)
                continue
            cmd = line[:4].upper()
            if cmd == "EHLO" or cmd == "HELO":
                self.say("250 fake-smtp")
            elif cmd == "DATA":
                data = True
                self.say("354 end with .")
            elif cmd == "QUIT":
                self.say("221 bye")
                return
            else:
                self.say("250 OK")

port = int(sys.argv[1]) if len(sys.argv) > 1 else 1025
print(f"fake SMTP listening on 127.0.0.1:{port}", flush=True)
socketserver.ThreadingTCPServer.allow_reuse_address = True
socketserver.ThreadingTCPServer(("127.0.0.1", port), H).serve_forever()
