#!/usr/bin/env python3
"""Prints its environment, working directory and umask, creates a file
(to check the umask with ls -l) then sleeps forever."""
import os, time
mask = os.umask(0); os.umask(mask)
print(f"cwd        = {os.getcwd()}", flush=True)
print(f"umask      = {mask:03o}", flush=True)
print(f"uid/user   = {os.getuid()} / {os.environ.get('USER')}", flush=True)
for k in ("STARTED_BY", "ANSWER", "GREETING"):
    print(f"env {k:<10}= {os.environ.get(k)}", flush=True)
with open("show_env_created_file", "w") as f:
    f.write("created by show_env.py\n")
print("created ./show_env_created_file (check its permissions with ls -l)", flush=True)
while True:
    time.sleep(1)
