# Validation and audit report

**Template version:** 3.0.0  
**Audit date:** 2026-08-03  
**Scope:** Runpod/Paddle documentation review, static code audit, local tests, syntax checks, JSON validation, and representative Markdown normalization.

## Material corrections

- Migrated provisioning to Runpod REST API v2 and current v2 field names.
- Removed the unsupported top-level endpoint `type` from create/update requests; the API response is validated as `QUEUE_BASED` instead.
- Removed the obsolete assumption that a v2 endpoint binds a template ID.
- Centralized template and endpoint container settings in one function.
- Added idempotent create/update behavior and release-rollout polling.
- Added transient control-plane retry/backoff and immutable image-reference support.
- Kept model initialization outside the handler and page processing through `predict_iter()`.
- Added fail-fast CUDA-device validation and explicit single-job concurrency.
- Added exact source-host allowlisting, public-IP validation, disabled proxy inheritance, bounded redirects, download limits, and safe logs.
- Added PDF magic, encryption, page-count, dimension, area, and `/UserUnit` validation.
- Added bounded Markdown artifact reads, cumulative per-job Markdown accounting, and an exact serialized-response limit.
- Added explicit job execution timeout, TTL, and best-effort cancellation in the smoke test.
- Added a true wall-clock download deadline and startup cleanup for stale interrupted-job directories.
- Changed FlashBoot to a privacy-first `OFF` default; it may be enabled only after live governance review.
- Tightened hostname syntax validation and release-rollout completion checks.
- Set the Runpod SDK log level to `ERROR` while retaining safe application metadata logs.
- Added container `pip check` and bytecode compilation.
- Corrected the production wording: direct Python integration is supported, while Paddle's inference-service/full-service topology is the upgrade path for higher performance and concurrency.

## KISS changes

The previous optional structured-page JSON and cross-page restructuring switches were removed. They were not needed by the Cloudflare extraction workflow and increased request surface, memory handling, tests, and maintenance.

The runtime contract is now:

```text
input:  one signed PDF URL
output: page-delimited Markdown + small metadata
```

The worker has two runtime modules and three direct runtime dependencies. It has no internal HTTP server, database, broker, persistent volume, second model service, arbitrary options, or per-job inference configuration.

## Local validation

The final package was checked with:

```text
python compilation: pass
unit tests: pass (25 tests)
shell syntax: pass
JSON examples: pass
Python line-length check (100): pass
representative Paddle Markdown normalization: pass
private-data scan: pass
Ruff: not run; the sandbox could not resolve the package index or files.pythonhosted.org. Static 100-column checks and Python compilation passed; `ruff==0.16.1` remains pinned for the build/CI environment
```

The supplied Paddle Markdown was used only for a local normalization check. Remote image authorization URLs were removed, the registry text remained present, and the private source was not copied into the package.

## Not validated here

These steps require the user's infrastructure and credentials:

- Pulling/building the multi-gigabyte Paddle image.
- Confirming the exact versioned Paddle tag in the registry and pinning its digest.
- Pushing and vulnerability-scanning the final worker image.
- Creating a live Runpod template and endpoint.
- Loading PaddleOCR-VL-1.6 on each selected GPU pool.
- Measuring VRAM, cold start, warm latency, throughput, and cost.
- Running the real representative PDF through the live endpoint.
- Testing a non-root derivative of the official image.
- Running a sustained 100-job qualification.

The code/configuration is therefore **ready for live production qualification**, not pre-certified for a specific deployment.

## Documentation alignment

- Runpod queue handlers use `runpod.serverless.start()` and recommend expensive model initialization outside the handler.
- Queue endpoints suit asynchronous and long-running work; `/run` avoids embedding PDFs in the limited request body.
- Endpoint timeouts, job TTL, privacy-first FlashBoot policy, worker caps, and queue-delay scaling are explicitly configured.
- Runpod REST API v2 template fields are spread into endpoint creation rather than referenced by template ID.
- PaddleOCR-VL-1.6 requires the complete layout-plus-VLM pipeline for full document parsing.
- `predict_iter()` is used for incremental, memory-conscious page processing.
- The official versioned offline NVIDIA image pattern is used, with production digest pinning required.
