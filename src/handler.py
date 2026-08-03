"""RunPod Queue-based Serverless worker for PaddleOCR-VL-1.6."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import paddle
from paddleocr import PaddleOCRVL

import runpod
from worker_core import (
    InputError,
    cleanup_stale_workdirs,
    combine_pages,
    enforce_output_limit,
    ensure_markdown_budget,
    load_settings,
    new_workdir,
    prepare_pdf,
    result_to_markdown,
    validate_job_input,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(message)s",
)
LOGGER = logging.getLogger("paddleocr-runpod")
SETTINGS = load_settings()
cleanup_stale_workdirs()


def verify_runtime_device() -> None:
    device = os.getenv("PADDLE_DEVICE", "gpu:0").strip().lower()
    if not device.startswith("gpu"):
        return
    if not paddle.is_compiled_with_cuda():
        raise RuntimeError("PaddlePaddle was not built with CUDA support")
    try:
        index = int(device.split(":", 1)[1]) if ":" in device else 0
    except ValueError as exc:
        raise RuntimeError("PADDLE_DEVICE must use the form gpu:N") from exc
    if index < 0 or index >= paddle.device.cuda.device_count():
        raise RuntimeError(f"Configured GPU index {index} is unavailable")


def log_event(event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    LOGGER.info(json.dumps(payload, separators=(",", ":"), default=str))


def build_pipeline() -> PaddleOCRVL:
    verify_runtime_device()
    started = time.perf_counter()
    try:
        pipeline = PaddleOCRVL(
            pipeline_version="v1.6",
            device=os.getenv("PADDLE_DEVICE", "gpu:0"),
            engine=os.getenv("PADDLE_ENGINE", "paddle"),
            use_doc_orientation_classify=SETTINGS.use_doc_orientation_classify,
            use_doc_unwarping=SETTINGS.use_doc_unwarping,
            use_layout_detection=SETTINGS.use_layout_detection,
            use_chart_recognition=SETTINGS.use_chart_recognition,
            use_seal_recognition=SETTINGS.use_seal_recognition,
        )
    except Exception as exc:
        log_event("pipeline_initialization_failed", error_type=type(exc).__name__)
        raise
    log_event(
        "pipeline_initialized",
        model="PaddleOCR-VL-1.6",
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    return pipeline


# RunPod and PaddleOCR both recommend loading expensive model state once.
PIPELINE = build_pipeline()


def progress(job: dict[str, Any], message: str) -> None:
    try:
        runpod.serverless.progress_update(job, message)
    except Exception:
        return


def handler(job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job.get("id", "unknown"))[:128]
    total_started = time.perf_counter()
    try:
        file_url = validate_job_input(job.get("input"))
    except InputError as exc:
        return {"ok": False, "error": {"code": exc.code, "message": exc.safe_message}}

    try:
        with new_workdir() as temporary:
            workdir = Path(temporary)
            progress(job, "Downloading and validating PDF")
            download_started = time.perf_counter()
            prepared = prepare_pdf(file_url, workdir, SETTINGS)
            download_ms = int((time.perf_counter() - download_started) * 1000)
            log_event(
                "job_started",
                job_id=job_id,
                byte_size=prepared.byte_size,
                page_count=prepared.page_count,
            )

            progress(job, "Running PaddleOCR-VL-1.6")
            inference_started = time.perf_counter()
            result_iterator = PIPELINE.predict_iter(
                input=str(prepared.path),
                temperature=SETTINGS.temperature,
                max_new_tokens=SETTINGS.max_new_tokens,
            )
            markdown_pages: list[str] = []
            for index, result in enumerate(result_iterator, start=1):
                page_markdown = result_to_markdown(
                    result,
                    workdir / "artifacts" / f"page-{index:04d}",
                    max_output_bytes=SETTINGS.max_output_bytes,
                )
                ensure_markdown_budget(
                    markdown_pages,
                    page_markdown,
                    index,
                    SETTINGS.max_output_bytes,
                )
                markdown_pages.append(page_markdown)

            inference_ms = int((time.perf_counter() - inference_started) * 1000)
            if not markdown_pages:
                raise InputError("EMPTY_OCR_RESULT", "PaddleOCR returned no results.")

            total_ms = int((time.perf_counter() - total_started) * 1000)
            output = enforce_output_limit(
                {
                    "ok": True,
                    "model": "PaddleOCR-VL-1.6",
                    "pipeline_version": "v1.6",
                    "worker_version": "3.0.0",
                    "source": {
                        "sha256": prepared.sha256,
                        "byte_size": prepared.byte_size,
                        "page_count": prepared.page_count,
                    },
                    "output_page_count": len(markdown_pages),
                    "markdown": combine_pages(markdown_pages),
                    "timings_ms": {
                        "download_and_validation": download_ms,
                        "inference": inference_ms,
                        "total": total_ms,
                    },
                },
                SETTINGS.max_output_bytes,
            )
            log_event(
                "job_completed",
                job_id=job_id,
                output_pages=len(markdown_pages),
                inference_ms=inference_ms,
                total_ms=total_ms,
            )
            return output
    except InputError as exc:
        log_event("job_rejected", job_id=job_id, code=exc.code)
        return {"ok": False, "error": {"code": exc.code, "message": exc.safe_message}}
    except Exception as exc:
        # Never log URLs, OCR content, extracted values, or tracebacks.
        log_event("job_failed", job_id=job_id, error_type=type(exc).__name__)
        raise RuntimeError("PADDLEOCR_INFERENCE_FAILED") from None


def serial_concurrency(_: int) -> int:
    """Keep one GPU-intensive OCR job active per worker."""
    return 1


runpod.serverless.start(
    {
        "handler": handler,
        "concurrency_modifier": serial_concurrency,
    }
)
