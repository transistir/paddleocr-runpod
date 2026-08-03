# PaddleOCR-VL-1.6 Queue Worker

Private Runpod Serverless queue worker for serial PDF parsing with the complete PaddleOCR-VL-1.6 pipeline.

- Input: one signed HTTPS PDF URL from an exact hostname allowlist.
- Output: page-delimited Markdown plus small source/timing metadata.
- Model: loaded once per worker.
- Storage: ephemeral local files only, startup stale-job cleanup, no network volume.
- Deployment: REST API v2 reusable template plus a queue endpoint using the same container configuration.
- Scope: low-to-medium-volume serial jobs after `PRODUCTION_CHECKLIST.md` passes.

REST API v2 endpoints do not bind a template ID, so the deployer spreads the same container settings into the endpoint request, validates that the API returns `QUEUE_BASED`, and waits for release rollout after updates.

Privacy default: `flashboot` is `OFF`. Enable state retention only after workload-specific review.
