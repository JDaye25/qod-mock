from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Dict

import requests


def pretty(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def expect_status(resp: requests.Response, expected: int, context: str) -> Dict[str, Any]:
    try:
        data = resp.json()
    except Exception:
        data = {"raw_text": resp.text}

    if resp.status_code != expected:
        raise RuntimeError(
            f"{context} failed: expected HTTP {expected}, got {resp.status_code}\n"
            f"Response: {json.dumps(data, indent=2)}"
        )
    return data


def request_json(
    method: str,
    url: str,
    *,
    headers: Dict[str, str] | None = None,
    json_body: Dict[str, Any] | None = None,
) -> tuple[requests.Response, Dict[str, Any] | None]:
    resp = requests.request(method=method, url=url, headers=headers, json=json_body, timeout=30)
    try:
        data = resp.json()
    except Exception:
        data = None
    return resp, data


def debug_checks(base_url: str, auth_headers: Dict[str, str]) -> None:
    print("\n== Debug checks ==")

    resp, _ = request_json("GET", f"{base_url}/_debug/env")
    env_data = expect_status(resp, 200, "GET /_debug/env")
    print("\n/_debug/env")
    pretty(env_data)

    resp, data = request_json("GET", f"{base_url}/_debug/protected")
    if resp.status_code != 401:
        raise RuntimeError(
            f"Expected GET /_debug/protected without header to return 401, got {resp.status_code}\n"
            f"Response: {json.dumps(data or {'raw_text': resp.text}, indent=2)}"
        )
    print("\n/_debug/protected without header")
    pretty(data or {"raw_text": resp.text})

    resp, _ = request_json("GET", f"{base_url}/_debug/protected", headers=auth_headers)
    protected_data = expect_status(resp, 200, "GET /_debug/protected with auth")
    print("\n/_debug/protected with header")
    pretty(protected_data)


def create_session(base_url: str, auth_headers: Dict[str, str]) -> str:
    print("\n== Create session ==")

    intent = {
        "text": "QoD auth end-to-end test",
        "target_p95_latency_ms": 120,
        "target_jitter_ms": 25,
        "duration_s": 15,
        "flow_label": "python-demo",
    }

    resp, data = request_json("POST", f"{base_url}/intent", json_body=intent)
    if resp.status_code != 401:
        raise RuntimeError(
            f"Expected POST /intent without auth to return 401, got {resp.status_code}\n"
            f"Response: {json.dumps(data or {'raw_text': resp.text}, indent=2)}"
        )
    print("\nPOST /intent without header")
    pretty(data or {"raw_text": resp.text})

    resp, _ = request_json("POST", f"{base_url}/intent", headers=auth_headers, json_body=intent)
    intent_data = expect_status(resp, 200, "POST /intent with auth")
    print("\nPOST /intent with header")
    pretty(intent_data)

    session_id = intent_data.get("session_id")
    if not session_id:
        raise RuntimeError("POST /intent succeeded but no session_id was returned")

    print(f"\nSession ID: {session_id}")
    return session_id


def post_telemetry(base_url: str, auth_headers: Dict[str, str], session_id: str) -> None:
    print("\n== Post telemetry ==")

    samples = [
        {"n": 100, "p50_ms": 40, "p95_ms": 90, "jitter_ms": 10, "notes": "sample-1"},
        {"n": 100, "p50_ms": 45, "p95_ms": 110, "jitter_ms": 12, "notes": "sample-2"},
        {"n": 100, "p50_ms": 42, "p95_ms": 100, "jitter_ms": 11, "notes": "sample-3"},
    ]

    for i, sample in enumerate(samples, start=1):
        payload = {"session_id": session_id, **sample}
        resp, _ = request_json("POST", f"{base_url}/telemetry", headers=auth_headers, json_body=payload)
        telemetry_data = expect_status(resp, 200, f"POST /telemetry sample {i}")
        print(f"\nTelemetry sample {i}")
        pretty(telemetry_data)


def finalize_proof(base_url: str, auth_headers: Dict[str, str], session_id: str) -> Dict[str, Any]:
    print("\n== Finalize proof ==")

    resp, _ = request_json("POST", f"{base_url}/proof/{session_id}/finalize", headers=auth_headers)
    final_data = expect_status(resp, 200, "POST /proof/{session_id}/finalize")
    pretty(final_data)
    return final_data


def verify_proof(base_url: str, auth_headers: Dict[str, str], session_id: str) -> Dict[str, Any]:
    print("\n== Verify proof ==")

    resp, _ = request_json("GET", f"{base_url}/proof/{session_id}/verify", headers=auth_headers)
    verify_data = expect_status(resp, 200, "GET /proof/{session_id}/verify")
    pretty(verify_data)
    return verify_data


def run_tamper_script(tamper_script: str, session_id: str) -> None:
    print("\n== Tamper DB row ==")

    if not os.path.exists(tamper_script):
        raise RuntimeError(f"Tamper script not found: {tamper_script}")

    env = os.environ.copy()
    env["SID"] = session_id

    result = subprocess.run(
        [sys.executable, tamper_script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)

    if result.returncode != 0:
        raise RuntimeError(f"Tamper script failed with exit code {result.returncode}")


def run_good_flow(base_url: str, token: str) -> None:
    auth_headers = {"Authorization": f"Bearer {token}"}

    debug_checks(base_url, auth_headers)
    session_id = create_session(base_url, auth_headers)
    post_telemetry(base_url, auth_headers, session_id)
    finalize_proof(base_url, auth_headers, session_id)
    verify_data = verify_proof(base_url, auth_headers, session_id)

    if verify_data.get("verified") is not True:
        raise RuntimeError(
            f"Expected verified=True for good flow, got verified={verify_data.get('verified')} "
            f"reason={verify_data.get('reason')}"
        )

    print("\nGOOD FLOW PASSED")
    print(f"Session ID: {session_id}")
    print(f"Verified: {verify_data.get('verified')}")
    print(f"Reason: {verify_data.get('reason')}")


def run_tamper_flow(base_url: str, token: str, tamper_script: str) -> None:
    auth_headers = {"Authorization": f"Bearer {token}"}

    debug_checks(base_url, auth_headers)
    session_id = create_session(base_url, auth_headers)
    post_telemetry(base_url, auth_headers, session_id)
    finalize_proof(base_url, auth_headers, session_id)

    verify_before = verify_proof(base_url, auth_headers, session_id)
    if verify_before.get("verified") is not True:
        raise RuntimeError(
            f"Expected verified=True before tamper, got verified={verify_before.get('verified')} "
            f"reason={verify_before.get('reason')}"
        )

    run_tamper_script(tamper_script, session_id)

    verify_after = verify_proof(base_url, auth_headers, session_id)
    if verify_after.get("verified") is not False:
        raise RuntimeError(
            f"Expected verified=False after tamper, got verified={verify_after.get('verified')} "
            f"reason={verify_after.get('reason')}"
        )

    if verify_after.get("reason") != "signature_mismatch":
        raise RuntimeError(
            f"Expected reason=signature_mismatch after tamper, got reason={verify_after.get('reason')}"
        )

    print("\nTAMPER FLOW PASSED")
    print(f"Session ID: {session_id}")
    print(f"Verified after tamper: {verify_after.get('verified')}")
    print(f"Reason after tamper: {verify_after.get('reason')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run QoD demo flows against the API.")
    parser.add_argument(
        "--mode",
        choices=["good", "tamper"],
        required=True,
        help="Which flow to run",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Base URL for the API",
    )
    parser.add_argument(
        "--token",
        default="dev-token-123",
        help="Bearer token to use",
    )
    parser.add_argument(
        "--tamper-script",
        default="tamper_db_signature_mismatch.py",
        help="Path to tamper script (used only in tamper mode)",
    )

    args = parser.parse_args()

    if args.mode == "good":
        run_good_flow(args.base_url.rstrip("/"), args.token)
    else:
        run_tamper_flow(args.base_url.rstrip("/"), args.token, args.tamper_script)


if __name__ == "__main__":
    main()
