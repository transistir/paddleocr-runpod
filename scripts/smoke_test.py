#!/usr/bin/env python3
"""Submit one asynchronous job and validate its response without printing OCR data."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / ".runpod-deploy.json"
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}
WORKER_VERSION = "3.1.0"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def endpoint_id() -> str:
    configured = os.getenv("RUNPOD_ENDPOINT_ID", "").strip()
    if configured:
        return configured
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        value = state.get("endpoint", {}).get("id")
        if value:
            return str(value)
    raise SystemExit("Set RUNPOD_ENDPOINT_ID or run scripts/deploy.py first")


def request_json(method: str, url: str, token: str, body: Any = None) -> Any:
    request = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"paddleocr-vl-smoke-test/{WORKER_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # The upstream error body can echo request data, including signed URLs.
        raise SystemExit(f"RunPod request failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit("RunPod request failed due to a network error") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SystemExit("RunPod returned an invalid JSON response") from exc


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer") from exc
    if value <= 0:
        raise SystemExit(f"{name} must be greater than zero")
    return value


def build_job_payload(
    source_url: str,
    execution_timeout_ms: int,
    ttl_ms: int,
) -> dict[str, Any]:
    if ttl_ms < execution_timeout_ms:
        raise SystemExit("SMOKE_TTL_MS must be >= SMOKE_EXECUTION_TIMEOUT_MS")
    return {
        "input": {
            "file_url": source_url,
        },
        "policy": {
            "executionTimeout": execution_timeout_ms,
            "ttl": ttl_ms,
        },
    }


def cancel_job(base: str, job_id: str, token: str) -> None:
    try:
        request_json("POST", f"{base}/cancel/{job_id}", token)
        print(f"Cancelled unfinished job {job_id}")
    except SystemExit as exc:
        print(f"Warning: unable to cancel unfinished job {job_id}: {exc}")


def main() -> None:
    load_dotenv(ROOT / ".env")
    token = required("RUNPOD_API_KEY")
    source_url = required("TEST_FILE_URL")
    endpoint = endpoint_id()
    timeout_seconds = positive_int("SMOKE_TIMEOUT_SECONDS", 1800)
    execution_timeout_ms = positive_int("SMOKE_EXECUTION_TIMEOUT_MS", 1_800_000)
    ttl_ms = positive_int("SMOKE_TTL_MS", 7_200_000)
    base = f"https://api.runpod.ai/v2/{endpoint}"
    submitted = request_json(
        "POST",
        f"{base}/run",
        token,
        build_job_payload(
            source_url,
            execution_timeout_ms,
            ttl_ms,
        ),
    )
    job_id = submitted.get("id") if isinstance(submitted, dict) else None
    if not job_id:
        raise SystemExit("RunPod did not return a job ID")
    print(f"Submitted job {job_id}")

    deadline = time.monotonic() + timeout_seconds
    last_status = None
    terminal_status = None
    try:
        while time.monotonic() < deadline:
            payload = request_json("GET", f"{base}/status/{job_id}", token)
            status = payload.get("status") if isinstance(payload, dict) else None
            if status != last_status:
                print(f"Status: {status}")
                last_status = status
            if status in TERMINAL:
                terminal_status = status
                if status != "COMPLETED":
                    raise SystemExit(f"Smoke test job ended with status {status}")
                output = payload.get("output")
                if not isinstance(output, dict) or output.get("ok") is not True:
                    code = None
                    if isinstance(output, dict):
                        code = (output.get("error") or {}).get("code")
                    raise SystemExit(f"Worker returned an error: {code or 'unknown'}")
                markdown = output.get("markdown")
                source = output.get("source")
                if not isinstance(markdown, str) or not markdown.strip():
                    raise SystemExit("Worker returned empty Markdown")
                if not isinstance(source, dict) or int(source.get("page_count", 0)) < 1:
                    raise SystemExit("Worker returned an invalid page count")
                print("Smoke test passed")
                print(f"Pages: {source['page_count']}")
                print(f"Markdown characters: {len(markdown)}")
                print(f"Total duration ms: {(output.get('timings_ms') or {}).get('total')}")
                return
            time.sleep(3)
        raise SystemExit(f"Smoke test timed out after {timeout_seconds} seconds")
    except KeyboardInterrupt as exc:
        raise SystemExit("Smoke test interrupted") from exc
    finally:
        if terminal_status is None:
            cancel_job(base, str(job_id), token)


if __name__ == "__main__":
    main()
