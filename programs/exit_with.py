#!/usr/bin/env python3
"""exit_with.py CODE DELAY : run DELAY seconds then exit with CODE.
Used to test exitcodes + autorestart (expected vs unexpected exits)."""
import sys, time
code = int(sys.argv[1]) if len(sys.argv) > 1 else 0
delay = float(sys.argv[2]) if len(sys.argv) > 2 else 3
print(f"exit_with: will exit with code {code} in {delay}s", flush=True)
time.sleep(delay)
sys.exit(code)
