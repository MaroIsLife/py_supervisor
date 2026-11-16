#!/usr/bin/env python3
"""Long running program: prints a counter every second (stdout) forever."""
import sys, time
name = sys.argv[1] if len(sys.argv) > 1 else "ticker"
i = 0
while True:
    print(f"[{name}] tick {i}", flush=True)
    i += 1
    time.sleep(1)
