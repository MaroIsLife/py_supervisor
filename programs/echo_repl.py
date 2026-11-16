#!/usr/bin/env python3
"""Interactive program to demo 'attach': echoes what you type in upper case."""
import sys
print("echo_repl ready, type something", flush=True)
for line in sys.stdin:
    line = line.rstrip("\n")
    if line == "quit":
        print("bye", flush=True)
        break
    print(f"ECHO: {line.upper()}", flush=True)
