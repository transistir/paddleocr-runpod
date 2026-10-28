# syntax=docker/dockerfile:1.7
# Official PaddleOCR-VL offline NVIDIA image with model assets included.
# Override BASE_IMAGE with a digest-pinned reference for approved releases.
# Pinada por digest: a base "latest-*" mudou entre 08/2026 e 10/2026 (paddleocr 3.6.x)
# e o handler deixou de funcionar em página densa. Esta é a camada do PoC (imagem 0.0.1).
ARG BASE_IMAGE=luandrodd/runpod-paddleocr-vl@sha256:85eee095f90aaedc962df220fe61ec9c94aab87168c62a22dfd2f893d60c9a75
FROM ${BASE_IMAGE}

USER root
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    RUNPOD_LOG_LEVEL=ERROR \
    TMPDIR=/tmp/paddleocr-runpod

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --requirement requirements.txt \
    && python -m pip check \
    && mkdir -p "${TMPDIR}"

COPY src ./src
RUN python -m compileall -q /app/src

CMD ["python", "-u", "/app/src/handler.py"]
