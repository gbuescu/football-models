"""Educational SHA-256d reference measurement. This is not a miner.

It times Python's hashlib (OpenSSL) hashing an 80-byte block header twice, on
one thread, with no network, no work from a pool and nothing submitted. Real
SHA-256 miners are far faster, and ASICs are faster again by many orders of
magnitude. The figure exists only to make the Route B arithmetic concrete.
"""

from __future__ import annotations

import hashlib
import os
import time


def measure(seconds: float = 3.0) -> dict:
    header = bytearray(os.urandom(80))
    sha256 = hashlib.sha256
    count = 0
    start = time.perf_counter()
    deadline = start + max(0.2, seconds)
    while time.perf_counter() < deadline:
        for nonce in range(10_000):
            header[76:80] = nonce.to_bytes(4, "little")
            sha256(sha256(header).digest()).digest()
        count += 10_000
    elapsed = time.perf_counter() - start
    return {
        "hashrate_hs": count / elapsed,
        "seconds": round(elapsed, 2),
        "threads": 1,
        "method": "Python hashlib double SHA-256 on one thread (educational reference, not a miner)",
        "result_kind": "local_reference",
    }
