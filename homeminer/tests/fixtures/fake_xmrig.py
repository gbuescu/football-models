#!/usr/bin/env python3
"""Test double that prints XMRig-style benchmark lines. It does no hashing.

Modes (FAKE_XMRIG_MODE): ok, mismatch, oom, hang, crash.
"""
import os
import sys
import time

args = dict(a.split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
mode = os.environ.get("FAKE_XMRIG_MODE", "ok")
log_path = args.get("--log-file")
size = args.get("--bench", "1M")
algo = args.get("--algo", "rx/0")


def emit(line, colored=None):
    print(colored or line, flush=True)
    if log_path:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


emit(f"[t]  bench    start benchmark hashes {size} algo {algo}")
if mode == "hang":
    time.sleep(120)
if mode == "oom":
    emit('[t]  nvidia   thread #0 failed with error <randomx_prepare>:85 "out of memory"')
    sys.exit(1)
if mode == "crash":
    sys.exit(3)
time.sleep(float(os.environ.get("FAKE_XMRIG_SECONDS", "0.5")))
green, red, clear = "\x1b[1;32m", "\x1b[1;31m", "\x1b[0m"
color = red if mode == "mismatch" else green
plain = "[t]  bench    benchmark finished in 0.500 seconds (2000000.0 h/s) hash sum = 0123456789ABCDEF"
emit(plain, plain.replace("hash sum = ", f"hash sum = {clear}{color}") + clear)
