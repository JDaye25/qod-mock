import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(base_url: str, method: str, path: str, timeout: float = 2.0):
    url = base_url + path
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8") if resp.length != 0 else ""
        data = json.loads(body) if body else None
        return resp.status, data


class SmokeTestQoD(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.port = get_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

        # Start uvicorn in background (NO --reload in tests/CI)
        cls.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backend.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(cls.port),
            ],
            cwd=str(cls.repo_root),
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        # Wait for /health to respond
        deadline = time.time() + 20
        last_err = None
        while time.time() < deadline:
            try:
                status, payload = http_json(cls.base_url, "GET", "/health", timeout=1.0)
                if status == 200 and isinstance(payload, dict):
                    return
            except Exception as e:
                last_err = e
                time.sleep(0.25)

        # If server never became healthy, dump logs for debugging
        logs = ""
        try:
            if cls.proc.stdout:
                logs = cls.proc.stdout.read()[-4000:]
        except Exception:
            pass
        raise RuntimeError(f"Server did not become healthy. Last error: {last_err}\nLogs:\n{logs}")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "proc", None):
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except Exception:
                cls.proc.kill()

    def test_health_endpoint(self):
        status, payload = http_json(self.base_url, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("status"), "ok")