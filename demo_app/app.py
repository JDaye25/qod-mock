from __future__ import annotations

import os
import random
import time

from fastapi import FastAPI

app = FastAPI(title="Demo App (Simulated Latency)")

BASE_MS = float(os.getenv("BASE_MS", "80"))   # baseline delay
JITTER_MS = float(os.getenv("JITTER_MS", "40"))  # randomness


@app.get("/ping")
def ping():
    delay_ms = max(0.0, random.gauss(BASE_MS, JITTER_MS))
    time.sleep(delay_ms / 1000.0)
    return {"ok": True, "delay_ms": round(delay_ms, 2)}