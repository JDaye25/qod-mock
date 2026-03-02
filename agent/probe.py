from __future__ import annotations

import argparse
import statistics
import time
from typing import List

import requests


def percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    values_sorted = sorted(values)
    k = (len(values_sorted) - 1) * pct
    f = int(k)
    c = min(f + 1, len(values_sorted) - 1)
    if f == c:
        return values_sorted[f]
    return values_sorted[f] + (values_sorted[c] - values_sorted[f]) * (k - f)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo-url", default="http://127.0.0.1:9000/ping")
    ap.add_argument("--fabric-url", default="http://127.0.0.1:8000")
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--sleep-ms", type=int, default=100)
    args = ap.parse_args()

    latencies_ms: List[float] = []

    for _ in range(args.n):
        t0 = time.perf_counter()
        r = requests.get(args.demo_url, timeout=10)
        r.raise_for_status()
        t1 = time.perf_counter()
        latencies_ms.append((t1 - t0) * 1000.0)
        time.sleep(args.sleep_ms / 1000.0)

    p50 = percentile(latencies_ms, 0.50)
    p95 = percentile(latencies_ms, 0.95)

    # A beginner-friendly "jitter proxy": stddev of latency samples
    jitter = statistics.pstdev(latencies_ms) if len(latencies_ms) >= 2 else 0.0

    payload = {
        "session_id": args.session_id,
        "n": args.n,
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "jitter_ms": round(jitter, 2),
        "notes": "local probe",
    }

    print("Computed:", payload)
    resp = requests.post(f"{args.fabric_url}/telemetry", json=payload, timeout=10)
    resp.raise_for_status()
    print("Posted telemetry OK:", resp.json())


if __name__ == "__main__":
    main()