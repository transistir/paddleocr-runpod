#!/usr/bin/env python3
"""Idempotently create/update a Runpod v2 template and queue endpoint."""

from __future__ import annotations

import contextlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
API_BASE = "https://api.runpod.io/v2"
STATE_FILE = ROOT / ".runpod-deploy.json"
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


def integer(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer") from exc
    if value < minimum:
        raise SystemExit(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise SystemExit(f"{name} must be <= {maximum}")
    return value


def csv(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


def validate_image_reference(image: str, *, variable_name: str = "RUNPOD_IMAGE") -> None:
    if "@sha256:" in image:
        digest = image.rsplit("@sha256:", 1)[-1]
        if len(digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in digest):
            raise SystemExit(f"{variable_name} contains an invalid sha256 digest")
        return
    final_component = image.rsplit("/", 1)[-1]
    if ":" not in final_component:
        raise SystemExit(f"{variable_name} must use a version tag or sha256 digest")
    if final_component.rsplit(":", 1)[-1].lower() == "latest":
        raise SystemExit(f"{variable_name} must not use the mutable :latest tag")


def deployment_image() -> str:
    build_image = required("DOCKER_IMAGE")
    image = os.getenv("RUNPOD_IMAGE", "").strip() or build_image
    validate_image_reference(image)
    return image


def api_request(method: str, path: str, token: str, body: dict[str, Any] | None = None) -> Any:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    attempts = 4
    for attempt in range(attempts):
        request = urllib.request.Request(
            f"{API_BASE}{path}",
            data=payload,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"paddleocr-vl-runpod-template/{WORKER_VERSION}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read().decode("utf-8")
                return json.loads(text) if text else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:2000]
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == attempts - 1:
                raise SystemExit(
                    f"Runpod API {method} {path} failed ({exc.code}): {detail}"
                ) from exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                delay = min(float(retry_after), 30.0) if retry_after else 2.0**attempt
            except ValueError:
                delay = 2.0**attempt
            time.sleep(delay)
        except urllib.error.URLError as exc:
            if attempt == attempts - 1:
                raise SystemExit(f"Runpod API {method} {path} failed: {exc.reason}") from exc
            time.sleep(2.0**attempt)
    raise AssertionError("unreachable")


def items_from_response(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("items", "data", "templates", "endpoints"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
    return []


def item_from_response(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        if isinstance(value.get("data"), dict):
            return value["data"]
        return value
    raise SystemExit("Runpod API returned an unexpected response")


def exact_named(items: list[dict[str, Any]], name: str, kind: str) -> dict[str, Any] | None:
    matches = [item for item in items if item.get("name") == name]
    if len(matches) > 1:
        raise SystemExit(f"Multiple {kind}s named {name!r} exist; resolve the ambiguity first.")
    return matches[0] if matches else None


def subset_equal(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    return all(current.get(key) == value for key, value in desired.items())


def runtime_env() -> dict[str, str]:
    defaults = {
        "PADDLE_DEVICE": "gpu:0",
        "PADDLE_ENGINE": "paddle",
        "MAX_INPUT_BYTES": "52428800",
        "MAX_PDF_PAGES": "25",
        "MAX_PDF_PAGE_EDGE_POINTS": "3600",
        "MAX_PDF_PAGE_AREA_POINTS": "10000000",
        "MAX_OUTPUT_BYTES": "8388608",
        "DOWNLOAD_TIMEOUT_SECONDS": "120",
        "MAX_REDIRECTS": "0",
        "USE_DOC_ORIENTATION_CLASSIFY": "false",
        "USE_DOC_UNWARPING": "false",
        "USE_LAYOUT_DETECTION": "true",
        "USE_CHART_RECOGNITION": "false",
        "USE_SEAL_RECOGNITION": "false",
        "TEMPERATURE": "0",
        "MAX_NEW_TOKENS": "8192",
        "LOG_LEVEL": "INFO",
        "RUNPOD_LOG_LEVEL": "ERROR",
    }
    values = {key: os.getenv(key, default).strip() for key, default in defaults.items()}
    values["ALLOWED_SOURCE_HOSTS"] = required("ALLOWED_SOURCE_HOSTS")
    return values


def container_config(image: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "image": image,
        "args": "",
        "disk": integer("RUNPOD_CONTAINER_DISK_GB", 50, 20),
        "ports": [],
        "env": runtime_env(),
    }
    registry = os.getenv("RUNPOD_CONTAINER_REGISTRY_AUTH_ID", "").strip()
    if registry:
        body["registry"] = registry
    return body


def build_template_body(image: str, template_name: str) -> dict[str, Any]:
    return {
        "name": template_name,
        **container_config(image),
        "category": "NVIDIA",
        "serverless": True,
        "public": False,
    }


def build_endpoint_body(image: str, endpoint_name: str) -> dict[str, Any]:
    pools = csv("RUNPOD_GPU_POOLS", "AMPERE_24,ADA_24")
    allowed_pools = {
        "AMPERE_16",
        "AMPERE_24",
        "ADA_24",
        "AMPERE_48",
        "ADA_48_PRO",
        "AMPERE_80",
        "ADA_80_PRO",
        "HOPPER_141",
    }
    if not 1 <= len(pools) <= 3:
        raise SystemExit("RUNPOD_GPU_POOLS must contain between one and three pools")
    unknown = [pool for pool in pools if pool not in allowed_pools]
    if unknown:
        raise SystemExit(f"Unknown RUNPOD_GPU_POOLS value: {unknown[0]}")
    workers_min = integer("RUNPOD_WORKERS_MIN", 0, 0)
    workers_max = integer("RUNPOD_WORKERS_MAX", 2, 1)
    if workers_min > workers_max:
        raise SystemExit("RUNPOD_WORKERS_MIN cannot exceed RUNPOD_WORKERS_MAX")
    scaler_type = os.getenv("RUNPOD_SCALER_TYPE", "QUEUE_DELAY").strip().upper()
    if scaler_type not in {"QUEUE_DELAY", "REQUEST_COUNT"}:
        raise SystemExit("RUNPOD_SCALER_TYPE must be QUEUE_DELAY or REQUEST_COUNT")
    flashboot = os.getenv("RUNPOD_FLASHBOOT", "OFF").strip().upper()
    if flashboot not in {"OFF", "FLASHBOOT", "PRIORITY_FLASHBOOT"}:
        raise SystemExit("RUNPOD_FLASHBOOT must be OFF, FLASHBOOT, or PRIORITY_FLASHBOOT")
    body: dict[str, Any] = {
        "name": endpoint_name,
        **container_config(image),
        "gpu": {"pools": pools, "count": 1},
        "workers": {"min": workers_min, "max": workers_max},
        "scaling": {
            "type": scaler_type,
            "value": integer("RUNPOD_SCALER_VALUE", 4, 1),
            "idleTimeout": integer("RUNPOD_IDLE_TIMEOUT_SECONDS", 60, 1, 3600),
        },
        "timeout": integer("RUNPOD_EXECUTION_TIMEOUT_MS", 1_800_000, 1_000),
        "flashboot": flashboot,
    }
    data_centers = csv("RUNPOD_DATA_CENTER_IDS")
    if data_centers:
        body["dataCenterIds"] = data_centers
    return body


def validate_existing_endpoint_type(endpoint: dict[str, Any]) -> None:
    endpoint_type = endpoint.get("type")
    if endpoint_type != "QUEUE_BASED":
        raise SystemExit(
            "An endpoint with the requested name exists but is not QUEUE_BASED; "
            "choose another RUNPOD_ENDPOINT_NAME or replace it manually."
        )


def extract_rollout(value: Any) -> dict[str, Any] | None:
    current = value
    if isinstance(current, dict) and isinstance(current.get("data"), dict):
        current = current["data"]
    if not isinstance(current, dict):
        return None
    rollout = current.get("rollout")
    return rollout if isinstance(rollout, dict) else None


def rollout_is_complete(rollout: dict[str, Any]) -> bool:
    try:
        total = int(rollout.get("workersTotal", 0))
        latest = int(rollout.get("workersOnLatest", 0))
    except (TypeError, ValueError):
        return False
    in_progress = rollout.get("inProgress")
    if in_progress is False:
        return True
    return total > 0 and latest >= total


def wait_for_endpoint_rollout(endpoint_id: str, token: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_state: tuple[Any, Any, Any] | None = None
    while True:
        response = api_request("GET", f"/serverless/{endpoint_id}/releases", token)
        rollout = extract_rollout(response)
        if rollout is None:
            raise SystemExit(
                "Runpod did not return an endpoint rollout summary after the update. "
                "Check the endpoint Releases view before sending production jobs."
            )
        state = (
            rollout.get("workersOnLatest"),
            rollout.get("workersTotal"),
            rollout.get("inProgress"),
        )
        if state != last_state:
            print(
                "Endpoint rollout: "
                f"{state[0] or 0}/{state[1] or 0} workers on latest release "
                f"(in progress: {bool(state[2])})"
            )
            last_state = state
        if rollout_is_complete(rollout):
            return
        if time.monotonic() >= deadline:
            raise SystemExit(
                "Endpoint rollout did not complete before the configured timeout. "
                "Check Runpod endpoint releases before sending production jobs."
            )
        time.sleep(5)


def main() -> None:
    load_dotenv(ROOT / ".env")
    token = required("RUNPOD_API_KEY")
    image = deployment_image()
    template_name = os.getenv("RUNPOD_TEMPLATE_NAME", "paddleocr-vl-1-6-serverless").strip()
    endpoint_name = os.getenv("RUNPOD_ENDPOINT_NAME", "paddleocr-vl-1-6").strip()

    templates = items_from_response(api_request("GET", "/templates", token))
    endpoints = items_from_response(api_request("GET", "/serverless", token))
    template = exact_named(templates, template_name, "template")
    endpoint = exact_named(endpoints, endpoint_name, "endpoint")

    desired_template = build_template_body(image, template_name)
    if template is None:
        print(f"Creating template {template_name!r} with Runpod REST API v2...")
        template = item_from_response(api_request("POST", "/templates", token, desired_template))
    elif subset_equal(template, desired_template):
        print(f"Template {template_name!r} is already up to date.")
    else:
        print(f"Updating template {template_name!r} ({template['id']})...")
        template = item_from_response(
            api_request("PATCH", f"/templates/{template['id']}", token, desired_template)
        )

    # In REST API v2 endpoints do not bind a template ID. The same container
    # configuration is deliberately applied to both resources from one function.
    desired_endpoint = build_endpoint_body(image, endpoint_name)
    endpoint_updated = False
    if endpoint is None:
        print(f"Creating queue endpoint {endpoint_name!r}...")
        endpoint = item_from_response(api_request("POST", "/serverless", token, desired_endpoint))
        validate_existing_endpoint_type(endpoint)
    else:
        validate_existing_endpoint_type(endpoint)
        if subset_equal(endpoint, desired_endpoint):
            print(f"Endpoint {endpoint_name!r} is already up to date.")
        else:
            print(f"Updating endpoint {endpoint_name!r} ({endpoint['id']})...")
            endpoint = item_from_response(
                api_request(
                    "PATCH",
                    f"/serverless/{endpoint['id']}",
                    token,
                    desired_endpoint,
                )
            )
            validate_existing_endpoint_type(endpoint)
            endpoint_updated = True

    template_id = str(template["id"])
    endpoint_id = str(endpoint["id"])
    if endpoint_updated:
        wait_for_endpoint_rollout(
            endpoint_id,
            token,
            integer("RUNPOD_ROLLOUT_TIMEOUT_SECONDS", 600, 30, 3600),
        )

    state = {
        "apiVersion": "v2",
        "workerVersion": WORKER_VERSION,
        "template": {"id": template_id, "name": template_name, "image": image},
        "endpoint": {"id": endpoint_id, "name": endpoint_name},
    }
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        STATE_FILE.chmod(0o600)

    request_urls = endpoint.get("requestUrls") if isinstance(endpoint, dict) else None
    async_url = request_urls.get("run") if isinstance(request_urls, dict) else None
    sync_url = request_urls.get("runSync") if isinstance(request_urls, dict) else None
    print(f"\nTemplate ID: {template_id}")
    print(f"Endpoint ID: {endpoint_id}")
    print(f"Async URL: {async_url or f'https://api.runpod.ai/v2/{endpoint_id}/run'}")
    print(f"Sync URL: {sync_url or f'https://api.runpod.ai/v2/{endpoint_id}/runsync'}")
    print(f"Status URL: https://api.runpod.ai/v2/{endpoint_id}/status/JOB_ID")
    print("\nRun the production qualification smoke test with: python scripts/smoke_test.py")


if __name__ == "__main__":
    main()
