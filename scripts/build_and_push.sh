#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${DOCKER_IMAGE:?Set DOCKER_IMAGE in .env}"
case "$DOCKER_IMAGE" in
  *@sha256:*) echo "DOCKER_IMAGE is a build tag and cannot be a digest. Use RUNPOD_IMAGE for deployment by digest." >&2; exit 1 ;;
  *:latest) echo "DOCKER_IMAGE must not use the mutable :latest tag." >&2; exit 1 ;;
  *:*) ;;
  *) echo "DOCKER_IMAGE must use an explicit version tag." >&2; exit 1 ;;
esac

BASE_IMAGE="${BASE_IMAGE:-ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddleocr-vl:paddleocr3.7-nvidia-gpu-offline}"

docker buildx build \
  --platform linux/amd64 \
  --pull \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  --tag "$DOCKER_IMAGE" \
  --load \
  .

if [[ "${1:-}" != "--build-only" ]]; then
  docker push "$DOCKER_IMAGE"
fi
