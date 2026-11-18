#!/usr/bin/env python3
"""Can only fail: writes an error and exits 1 immediately (-> BACKOFF then FATAL)."""
import sys, time
print("crasher: something went terribly wrong", file=sys.stderr, flush=True)
time.sleep(5)
sys.exit(1)
