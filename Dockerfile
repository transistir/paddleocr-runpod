# syntax=docker/dockerfile:1.7
# Official PaddleOCR-VL 3.7 offline NVIDIA image with model assets included.
ARG BASE_IMAGE=ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddleocr-vl:paddleocr3.7-nvidia-gpu-offline
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
