# Runpod Serverless PaddleOCR-VL-1.6 worker

A small Runpod **queue-based Serverless** worker that accepts one signed HTTPS PDF URL and returns page-delimited Markdown from the complete PaddleOCR-VL-1.6 pipeline.

## Status and intended load

The repository is a **production-hardened deployment candidate** for serial, low-to-medium-volume OCR. It is not a claim that an untested deployment is production-certified. Complete [PRODUCTION_CHECKLIST.md](PRODUCTION_CHECKLIST.md) on the actual Runpod account, image digest, GPU pools, and representative documents before processing production data.

Paddle supports direct Python integration for real applications and recommends VLM/full-service deployment when higher speed, concurrency, resource isolation, or stability is required. This worker deliberately uses the direct complete pipeline because one synchronous handler per GPU is the simplest reliable architecture for the current workload. It does not run only the underlying VLM; layout analysis and VLM recognition remain together.

## KISS design

- One queue handler and one globally initialized Paddle pipeline.
- One accepted input field: `file_url`.
- One primary output: Markdown.
- One job at a time per GPU worker, explicitly enforced with `concurrency_modifier`.
- Ephemeral local files only, with startup cleanup for interrupted prior jobs.
- No FastAPI, database, broker, network volume, user prompts, or dynamic inference options.
- Horizontal scaling is handled by Runpod workers, not by application concurrency.

```text
Cloudflare Worker / trusted client
  └─ signed HTTPS PDF URL
       └─ Runpod /run queue
            └─ one GPU worker
                 ├─ validate URL and PDF
                 ├─ run complete PaddleOCR-VL-1.6
                 ├─ return page-delimited Markdown
                 └─ delete temporary files
```

## Repository

```text
Dockerfile                 Official Paddle offline image + worker layer
src/handler.py             Runpod handler and model lifecycle
src/worker_core.py         URL/PDF validation, download, normalization
scripts/build_and_push.sh  Versioned image build and push
scripts/deploy.py          Idempotent Runpod REST API v2 deployment
scripts/smoke_test.py      Safe asynchronous live qualification
runpod/*.json.example      Readable API v2 request examples
tests/                     Local unit tests
PRODUCTION_CHECKLIST.md    Required live qualification
VALIDATION.md              Audit and local validation record
```

## Prerequisites

- Docker with Buildx
- Python 3.10 or newer
- A container registry
- A Runpod API key with only the required template/serverless permissions
- A controlled HTTPS host that serves single-object signed PDF URLs

The official Paddle GPU image requires an NVIDIA driver compatible with CUDA 12.6 or newer. The default endpoint uses the Runpod `AMPERE_24` and `ADA_24` pools. Every enabled pool must be qualified before production.

## Build and deploy

```bash
cp .env.example .env
```

Set at minimum:

```dotenv
DOCKER_IMAGE=ghcr.io/YOUR_ORG/runpod-paddleocr-vl:3.0.0
RUNPOD_API_KEY=...
ALLOWED_SOURCE_HOSTS=your-worker.example.workers.dev
```

Run local checks:

```bash
python -m unittest discover -s tests -v
python -m compileall -q src scripts tests
bash -n scripts/build_and_push.sh
```

Build and push:

```bash
./scripts/build_and_push.sh
```

Create or update the reusable template and queue endpoint:

```bash
python scripts/deploy.py
```

For an approved release:

1. Pin `BASE_IMAGE` to the official Paddle image digest.
2. Keep `DOCKER_IMAGE` as a versioned build tag.
3. Set `RUNPOD_IMAGE` to the pushed worker image's immutable `@sha256:...` digest.
4. Rerun `scripts/deploy.py` and complete the live checklist.

### Why both a template and an endpoint are created

Runpod REST API v2 templates are reusable container presets, but v2 endpoint creation does not accept a template ID. The deployer therefore uses one `container_config()` function to create/update the human-reusable template and to spread the same fields into the actual endpoint request. This avoids configuration drift while preserving a usable Runpod template.

The v2 endpoint create request does not include a top-level endpoint `type`; the response contains it. The deployer sends only documented request fields and fails unless the returned or existing endpoint is `QUEUE_BASED`. Container or environment updates create endpoint releases, so the deployer polls the releases endpoint until rollout is complete.

Runpod currently labels REST API v2 as beta while directing new integrations away from deprecated REST API v1. Re-run the qualification checks after control-plane API changes.

## Request

Use asynchronous `/run`:

```json
{
  "input": {
    "file_url": "https://your-worker.example.workers.dev/private/document.pdf?signature=..."
  },
  "policy": {
    "executionTimeout": 1800000,
    "ttl": 7200000
  }
}
```

Only `file_url` is accepted inside `input`. The worker intentionally omits base64 input because it increases payload size and works poorly with Runpod request limits.

The source URL must:

- use HTTPS on port 443;
- use an exact hostname in `ALLOWED_SOURCE_HOSTS`;
- resolve only to public IP addresses;
- contain no user information or fragment;
- stay valid through the worst-case queue wait, cold start, and initial download;
- point to a PDF within the byte, page, and page-dimension limits.

Align the signed-URL lifetime with the submitted job TTL. A shorter URL can cause a queued job to fail before download; an unnecessarily long URL weakens data minimization.

## Response

```json
{
  "ok": true,
  "model": "PaddleOCR-VL-1.6",
  "pipeline_version": "v1.6",
  "worker_version": "3.0.0",
  "source": {
    "sha256": "...",
    "byte_size": 12345,
    "page_count": 1
  },
  "output_page_count": 1,
  "markdown": "<!-- PAGE 1 -->\n\n...",
  "timings_ms": {
    "download_and_validation": 100,
    "inference": 3000,
    "total": 3200
  }
}
```

Markdown is never silently truncated. If the serialized response exceeds `MAX_OUTPUT_BYTES`, the job returns a safe `OUTPUT_TOO_LARGE` error.

Expected input/processing errors return:

```json
{
  "ok": false,
  "error": {
    "code": "INVALID_PDF",
    "message": "The downloaded file is not a PDF."
  }
}
```

Unexpected runtime failures are raised generically so Runpod marks the job failed without exposing source URLs or OCR content.

## Security and privacy

- The Runpod API key is used only by local deployment and smoke-test scripts.
- The worker accepts downloads only from exact configured hostnames.
- HTTPS, standard port, public DNS resolution, URL length, user-info, and fragment checks are enforced.
- Redirects are disabled by default; every redirect is revalidated if explicitly enabled.
- Download time is bounded by a wall-clock deadline, not only a per-read timeout.
- Ambient proxy environment variables are ignored for downloads.
- Downloads are streamed with byte and timeout limits.
- PDF magic, parser validity, encryption, page count, page dimensions, and `/UserUnit` are checked before inference.
- Markdown artifacts are size-checked before being read into memory.
- Cumulative Markdown size is enforced while pages are produced, before unbounded accumulation.
- Remote image tags and embedded Markdown images are removed from the returned Markdown.
- Application logs contain only job IDs, counts, timings, model metadata, and safe error categories.
- The Runpod SDK log level defaults to `ERROR` to reduce platform-side input/output diagnostics.
- URLs, signatures, document text, identifiers, returned Markdown, and tracebacks are not logged.
- Each job uses a unique temporary directory that is deleted automatically.
- Stale `job-*` directories from an interrupted prior process are removed at worker startup.
- No persistent or network volume is configured.

The exact hostname allowlist assumes the listed host is controlled and trusted. DNS validation reduces SSRF risk but is not a substitute for controlling the source host and using single-object signed URLs.

Runpod keeps asynchronous job results available for a limited period after completion (currently 30 minutes for `/run`). This is not a zero-retention design. Retrieve results promptly and include Runpod processing/retention in the data-governance assessment.

The Dockerfile retains the official image's root runtime because Paddle's documented GPU container examples use root and the bundled model-file permissions have not been live-qualified under a non-root derivative. Where policy requires non-root containers, qualify a derivative image before adoption rather than changing the user blindly.

## Important defaults

| Setting | Default | Purpose |
|---|---:|---|
| `RUNPOD_GPU_POOLS` | `AMPERE_24,ADA_24` | prioritized 24 GB GPU pools |
| `RUNPOD_WORKERS_MIN` | `0` | scale to zero |
| `RUNPOD_WORKERS_MAX` | `2` | cost and concurrency cap |
| `RUNPOD_IDLE_TIMEOUT_SECONDS` | `60` | brief warm reuse |
| `RUNPOD_EXECUTION_TIMEOUT_MS` | `1800000` | 30-minute endpoint limit |
| `RUNPOD_FLASHBOOT` | `OFF` | privacy-first default; enable only after qualification |
| `MAX_INPUT_BYTES` | `52428800` | 50 MiB PDF cap |
| `MAX_PDF_PAGES` | `25` | page cap |
| `MAX_PDF_PAGE_EDGE_POINTS` | `3600` | pathological-page guard |
| `MAX_PDF_PAGE_AREA_POINTS` | `10000000` | pathological-page guard |
| `MAX_OUTPUT_BYTES` | `8388608` | 8 MiB serialized response cap |
| `MAX_REDIRECTS` | `0` | safest redirect policy |
| `MAX_NEW_TOKENS` | `8192` | bounded VLM generation per page |
| `RUNPOD_LOG_LEVEL` | `ERROR` | suppress verbose SDK diagnostics |

Orientation correction, unwarping, chart recognition, and seal recognition are fixed endpoint policy values. Keep them disabled until representative measurements show a need; callers cannot change them per job. FlashBoot is also disabled by default because it retains worker state for faster revival; enable it only after confirming that its retention behavior is acceptable for the document-governance policy.

## Live smoke test

Set a non-sensitive signed test PDF:

```dotenv
TEST_FILE_URL=https://your-allowed-host.example/private/test.pdf?signature=...
```

Run:

```bash
python scripts/smoke_test.py
```

The script submits through `/run`, sets explicit execution/TTL limits, polls `/status`, verifies non-empty Markdown and page count, and never prints the signed URL or OCR content. It attempts `/cancel` if the client times out or is interrupted.

## Operations

- Start with `workers.min=0` and a low `workers.max`.
- Monitor queue delay, failed jobs, cold starts, execution time, and cost per page.
- Run at least 100 representative jobs before approval.
- Roll back by restoring the previous worker image digest and rerunning deployment.
- Re-run the production checklist after changes to Paddle, the base image, dependencies, GPU pools, or Runpod APIs.

Runpod also automatically reduces `workers.max` after prolonged endpoint inactivity. For an infrequently used production endpoint, monitor this control-plane change and restore the approved maximum before expected traffic.

## Scaling threshold

Keep this one-process worker while serial latency and throughput meet the service objective. Move to Paddle's dedicated VLM or full-service topology when measured concurrency, latency, VRAM, or stability requirements justify the extra services. Preserve the external `file_url` → Markdown contract during that migration.
