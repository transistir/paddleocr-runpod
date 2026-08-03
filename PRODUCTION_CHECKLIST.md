# Production qualification checklist

This repository is a hardened candidate for serial, low-to-medium-volume OCR. A deployment is not approved until every applicable item passes on the actual account, image digest, GPU pools, and representative documents.

## Build and supply chain

- [ ] Confirm the selected versioned official Paddle base-image tag exists.
- [ ] Record and pin the official Paddle base-image digest.
- [ ] Build for `linux/amd64`.
- [ ] Run `pip check` and the full local test suite.
- [ ] Scan the final image for critical/high vulnerabilities.
- [ ] Push a versioned worker image.
- [ ] Set `RUNPOD_IMAGE` to the approved worker `sha256` digest.
- [ ] Record source revision, base digest, worker digest, and dependency versions.

## Runpod control plane

- [ ] Use a restricted provisioning API key.
- [ ] Deploy with REST API v2.
- [ ] Confirm the returned endpoint type is `QUEUE_BASED`.
- [ ] Confirm the deployer reports completed release rollout after image/environment updates.
- [ ] Verify no network volume or exposed ports are attached.
- [ ] Confirm worker min/max, queue-delay scaling, idle timeout, execution timeout, and FlashBoot match policy.
- [ ] Confirm `RUNPOD_FLASHBOOT=OFF` for sensitive workloads, or document approval to enable state retention.
- [ ] Account for Runpod automatic inactivity scale-down of `workers.max`.

## Functional qualification

- [ ] Test one-page and multi-page PDFs.
- [ ] Test the real representative property-registry PDF.
- [ ] Confirm expected page markers and non-empty Markdown.
- [ ] Confirm malformed, encrypted, oversized, over-page-limit, and pathological-dimension PDFs are rejected.
- [ ] Confirm an oversized response returns `OUTPUT_TOO_LARGE` without truncation.
- [ ] Confirm temporary job files are removed after success and failure.
- [ ] Kill a worker during processing and confirm stale `job-*` directories are removed on restart.
- [ ] Verify slow/trickling downloads fail at the wall-clock timeout.
- [ ] Confirm the live smoke test cancels an unfinished job after client timeout/interruption.

## GPU and performance

- [ ] Verify startup and inference on every enabled GPU pool.
- [ ] Record peak VRAM on the largest supported representative PDF.
- [ ] Remove pools that fail to initialize or show unstable performance.
- [ ] Measure image pull, cold start, warm latency, and cost per page. If FlashBoot is approved, measure revival behavior separately.
- [ ] Confirm the 30-minute execution timeout and selected job TTL are appropriate.
- [ ] Run at least 100 representative jobs without a crash or leak trend.
- [ ] Submit concurrent jobs up to `workers.max` and verify exactly one active OCR job per worker.

## Source URL and SSRF controls

- [ ] Restrict `ALLOWED_SOURCE_HOSTS` to exact controlled hostnames.
- [ ] Verify the source host resolves only to expected public addresses.
- [ ] Use single-object HTTPS signed URLs.
- [ ] Align URL expiry with worst-case queue wait, cold start, and download while minimizing exposure.
- [ ] Keep redirects disabled unless a measured requirement exists.
- [ ] Verify logs never contain full URLs, query signatures, OCR text, identifiers, or Markdown.
- [ ] Confirm `RUNPOD_LOG_LEVEL=ERROR` and inspect platform logs during success and failure tests.

## Privacy and security

- [ ] Document Runpod as a data processor and document current job-result retention.
- [ ] Confirm the workload permits third-party GPU processing.
- [ ] Keep all Runpod API keys out of the worker environment.
- [ ] Use a separate endpoint-invocation key from the provisioning key where practical.
- [ ] Confirm no sensitive real document is bundled in the image or repository.
- [ ] Document acceptance of the official image's root runtime, or qualify a tested non-root derivative if policy requires it.

## Operations

- [ ] Add a synthetic canary with explicit execution timeout, TTL, and cancellation.
- [ ] Alert on failure rate, timeouts, queue delay, cold-start regression, and spend.
- [ ] Set a budget-appropriate maximum worker count and billing alerts.
- [ ] Document rollback to the previous worker image digest.
- [ ] Re-run this checklist after Paddle, base image, dependency, GPU-pool, or Runpod API changes.

## Scale threshold

Move to Paddle's dedicated VLM/full-service deployment when sustained traffic, concurrency, latency objectives, or worker stability exceed what this serial queue worker can meet. Do not add that topology before measurements justify it.
