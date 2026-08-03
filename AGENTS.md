# AGENTS.md

This file applies to the entire repository. Add a more specific `AGENTS.md` only when a
subdirectory genuinely needs different instructions; the nearest file takes precedence.

## Mission

Maintain a small, secure, queue-based RunPod Serverless worker that accepts one trusted
HTTPS PDF URL and returns page-delimited Markdown from the complete PaddleOCR-VL-1.6
pipeline.

Preserve the project’s KISS architecture:

- one RunPod handler;
- one globally initialized Paddle pipeline per worker;
- one active OCR job per GPU worker;
- one input field, `file_url`;
- one primary output, Markdown;
- ephemeral local files only;
- horizontal scaling through RunPod, not application-level concurrency;
- no FastAPI, database, broker, persistent volume, user-defined prompt, or dynamic
  per-request inference configuration.

Read these before making nontrivial changes:

- `README.md` for the public contract and operating model;
- `PRODUCTION_CHECKLIST.md` for release qualification;
- `VALIDATION.md` for the latest audit and validation record;
- `.env.example` for all supported configuration;
- `.github/workflows/ci.yml` for the authoritative fast-check sequence.

## Repository map

- `src/handler.py`: RunPod lifecycle, global Paddle pipeline, job handler, safe logging.
- `src/worker_core.py`: settings, input validation, SSRF controls, download, PDF checks,
  temporary files, Markdown normalization, and output-size enforcement.
- `scripts/deploy.py`: idempotent RunPod REST API v2 template and endpoint deployment.
- `scripts/smoke_test.py`: asynchronous live qualification against a deployed endpoint.
- `scripts/build_and_push.sh`: local versioned image build and push helper.
- `Dockerfile`: thin worker layer on the official offline Paddle GPU image.
- `docker-compose.yml`: local GPU development only.
- `tests/`: deterministic unit tests; external APIs and GPUs are not required.
- `.github/workflows/`: CI, Compose validation, and release image publication.
- `runpod/*.json.example`: human-readable RunPod request examples.

Keep responsibilities in these existing modules unless a split clearly reduces complexity.
Do not introduce a framework or abstraction layer for a single implementation.

## Setup and fast checks

Use Python 3.10 or newer; CI currently validates Python 3.10.

```bash
python -m pip install --upgrade pip
python -m pip install --requirement requirements-dev.txt
```

Run the same fast checks as CI before finishing any code change:

```bash
ruff check .
ruff format --check .
python -m compileall -q src scripts tests
python -m unittest discover -s tests -v
bash -n scripts/build_and_push.sh
python - <<'PY'
import glob
import json

files = glob.glob("runpod/*.example") + ["test_input.json.example"]
for path in files:
    with open(path, encoding="utf-8") as source:
        json.load(source)
print(f"validated {len(files)} JSON examples")
PY
docker compose config
```

Use `ruff format .` only when formatting changes are intended.

Do not run these expensive or external operations unless the task requires them and the
necessary credentials/infrastructure are available:

```bash
docker compose --env-file .env up
./scripts/build_and_push.sh
python scripts/deploy.py
python scripts/smoke_test.py
```

A Docker build pulls a very large GPU image. Do not build it merely to validate a small
Python or documentation change.

## Coding conventions

- Use Python type annotations and `from __future__ import annotations` in Python modules.
- Prefer standard-library functionality when it keeps the code clear.
- Keep dependencies minimal and pinned in `requirements*.txt`.
- Use small, explicit functions and frozen dataclasses for configuration/value objects.
- Use `pathlib.Path`, context managers, and deterministic cleanup for filesystem work.
- Keep the handler thin; reusable validation and transformation logic belongs in
  `worker_core.py`.
- Keep deployment API details in `scripts/deploy.py`, not in runtime worker code.
- Preserve JSON-compatible public response shapes.
- Avoid hidden fallback behavior, silent truncation, and broad exception swallowing.
- Comments should explain security, compatibility, or non-obvious design decisions—not
  restate straightforward code.

## Security and privacy invariants

Treat these as release-blocking requirements:

- Accept only the documented `{"file_url": "..."}` input shape.
- Require HTTPS, standard port 443, exact hostname allowlisting, and public DNS results.
- Keep redirects disabled by default; revalidate every redirect when enabled.
- Ignore ambient proxy variables for document downloads.
- Stream downloads with byte and wall-clock limits.
- Validate PDF magic, encryption, page count, page dimensions, page area, and `/UserUnit`
  before inference.
- Never log or expose source URLs, query signatures, PDF contents, OCR text, Markdown,
  personal identifiers, API keys, or secrets.
- Expected client/input failures return a safe `InputError` code and message.
- Unexpected failures must remain generic to clients and RunPod job output.
- Use one unique temporary directory per job and remove it after success or failure.
- Keep startup cleanup limited to the worker’s own stale `job-*` directories.
- Do not add persistent or network storage without an explicit architecture and privacy
  review.
- Do not place `RUNPOD_API_KEY`, registry credentials, or source-host signing secrets in
  the worker environment or image.
- Do not commit `.env`, `.runpod-deploy.json`, real documents, OCR output, tokens, or
  private fixture data.

When modifying URL validation, download handling, PDF parsing, logging, temporary files,
or output serialization, add focused regression tests for both accepted and rejected
cases.

## PaddleOCR invariants

- Use the complete `PaddleOCRVL` pipeline, not only the underlying vision-language model.
- Initialize the expensive pipeline once at module startup, outside the handler.
- Keep `predict_iter()` so pages are processed incrementally.
- Use Paddle’s `save_to_markdown()` path and sanitize its generated Markdown.
- Keep layout detection enabled by default.
- Keep orientation classification, unwarping, chart recognition, and seal recognition
  disabled until representative measurements justify enabling them.
- Keep inference deterministic by default (`TEMPERATURE=0`).
- Enforce `MAX_NEW_TOKENS`, cumulative Markdown limits, and final serialized response
  limits; never silently truncate.
- Preserve explicit page markers in combined Markdown.
- Verify current official PaddleOCR documentation before changing pipeline parameters,
  engine selection, image tags, or runtime requirements.

## RunPod invariants

- The production endpoint is queue-based and uses asynchronous `/run`.
- Enforce one active OCR request per GPU worker through `concurrency_modifier`.
- Keep RunPod API access out of runtime code except the Serverless SDK used by the
  handler.
- `scripts/deploy.py` must remain idempotent and use the current documented RunPod REST
  API v2 contracts.
- Validate that an existing endpoint is `QUEUE_BASED` before updating it.
- Poll release rollout after endpoint image or environment changes.
- Keep `workers.min=0` and a bounded `workers.max` unless measured service objectives
  require a change.
- Keep FlashBoot off by default; enabling retained state requires an explicit privacy
  decision.
- Do not attach exposed ports or a network volume.
- Verify current official RunPod documentation before changing endpoint fields, GPU pool
  identifiers, scaling behavior, timeouts, release polling, or job APIs.

## Public contract changes

The current request contract is intentionally narrow:

```json
{"input": {"file_url": "https://trusted.example/document.pdf?..."}}
```

A successful result contains model/version metadata, source hash/size/page count,
page-delimited Markdown, and timings. Expected validation failures return:

```json
{"ok": false, "error": {"code": "...", "message": "..."}}
```

Changing either contract requires all of the following in the same change:

- implementation updates;
- unit tests;
- `README.md` examples;
- `test_input.json.example` and relevant `runpod/*.json.example` files;
- smoke-test assertions when applicable;
- a compatibility note in the pull request.

Do not add arbitrary per-request model settings. New behavior should normally be fixed
endpoint policy expressed through validated environment variables.

## Tests

- Every behavioral change needs a focused test.
- Keep unit tests deterministic and offline.
- Mock DNS, HTTP responses, time, filesystem boundaries, and RunPod control-plane calls.
- Do not require Paddle, CUDA, Docker, network access, or secrets for the normal test
  suite.
- Test safe error codes/messages rather than implementation tracebacks.
- For security controls, test both the allowed path and representative bypass attempts.
- When changing deployment request bodies, test serialization, validation, idempotency,
  and ambiguous-existing-resource failures.
- When fixing a bug, first add a regression test that would have caught it.

The live smoke test is supplementary; it does not replace unit tests or the production
qualification checklist.

## Docker and release rules

- Build only `linux/amd64` for RunPod.
- Prefer an official Paddle base image pinned by digest for approved releases.
- Never publish or deploy `:latest`.
- Publish immutable semantic-version and commit-SHA tags.
- Deploy the worker image by digest through `RUNPOD_IMAGE`.
- Do not overwrite an existing release tag.
- Keep Docker Hub credentials only in GitHub Actions secrets.
- Preserve SBOM and provenance generation in the release workflow.
- Do not weaken CI, release immutability, or supply-chain checks to make a build pass.

When changing the worker version, update every user-visible or request-identifying version
consistently, including at least:

- `src/handler.py` response metadata;
- `src/worker_core.py` download user agent;
- `scripts/deploy.py` worker version/user agent;
- image examples and documentation where applicable.

Search the repository for the old version before completing the change.

## Documentation

Keep `AGENTS.md` focused on instructions for agents. Keep human setup, API usage, and
operations in `README.md`; keep live approval gates in `PRODUCTION_CHECKLIST.md`.

Update documentation whenever defaults, environment variables, contracts, commands,
RunPod resources, security behavior, or operational assumptions change. Use relative
links for repository files.

Do not call a deployment “production certified” until the checklist has been completed on
the actual image digest, account, GPU pools, and representative documents.

## Change discipline

- Inspect the relevant implementation, tests, and documentation before editing.
- Prefer the smallest coherent patch that solves the task.
- Do not refactor unrelated code while fixing a focused issue.
- Preserve backward compatibility unless the task explicitly authorizes a contract
  change.
- Never modify or delete existing RunPod resources from tests.
- Do not deploy, publish images, rotate secrets, or run destructive Git/RunPod operations
  without explicit authorization.
- Never commit generated state or credentials.

Before reporting completion:

1. review the final diff for scope and accidental sensitive data;
2. run all applicable fast checks;
3. state which checks ran and their results;
4. state clearly which GPU, Docker, live RunPod, or smoke tests were not run;
5. summarize any contract, configuration, security, or operational impact.
