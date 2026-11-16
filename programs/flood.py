#!/usr/bin/env python3
"""Generates a LOT of output on stdout and stderr (stress test)."""
import sys
i = 0
while True:
    sys.stdout.write(f"flood line {i} " + "x" * 100 + "\n")
    if i % 10 == 0:
        sys.stderr.write(f"flood err {i}\n")
    i += 1
