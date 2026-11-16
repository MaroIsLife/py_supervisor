#!/usr/bin/env python3
"""Ignores SIGTERM, exits cleanly on SIGUSR1.
 - stopsignal: USR1 -> stops gracefully at once
 - stopsignal: TERM -> ignored, taskmaster SIGKILLs it after stoptime"""
import signal, sys, time

def on_usr1(sig, frame):
    print("stubborn: got SIGUSR1, exiting gracefully", flush=True)
    sys.exit(0)

signal.signal(signal.SIGTERM, lambda s, f: print("stubborn: SIGTERM? I don't care", flush=True))
signal.signal(signal.SIGUSR1, on_usr1)
print("stubborn: running", flush=True)
while True:
    time.sleep(1)
