"""Time the dig paths against live cosine.club/Discogs on a throwaway DB.

    uv run python scripts/bench.py ["Artist - Track" ...]

Runs the fast dig, then the deep dig twice (cold, then warm from the Discogs
cache) and prints wall times. Nothing is exported; the real DB is untouched.
"""
from __future__ import annotations

import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

from cosine_digger import api

SEEDS = sys.argv[1:] or ["Vladislav Delay - Ranta", "Conoco - Koski", "Humbros - Ressac"]


def run(label: str, conn, deep: bool) -> None:
    t0 = time.perf_counter()
    out = api.quick(SEEDS, conn=conn, export_playlist=False, record=False, deep=deep)
    wall = time.perf_counter() - t0
    print(f"{label:<12} {wall:6.1f} s  picks={len(out['picks']):<3} {out['timings']}"
          + (f"  error={out['error']}" if out.get("error") else ""), flush=True)


with tempfile.TemporaryDirectory() as tmp, closing(api.open_db(Path(tmp) / "bench.db")) as conn:
    print(f"seeds: {SEEDS}")
    run("fast", conn, deep=False)
    run("deep cold", conn, deep=True)
    run("deep warm", conn, deep=True)
