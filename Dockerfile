ARG PYTHON_BASE_IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
FROM ${PYTHON_BASE_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY requirements/constraints.txt ./requirements/constraints.txt
COPY src ./src
RUN python -m pip install \
    --constraint requirements/constraints.txt \
    setuptools==84.2.1 \
    && python -m pip wheel \
    --no-build-isolation \
    --constraint requirements/constraints.txt \
    --wheel-dir /wheels \
    .

FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 cryptolab \
    && useradd --uid 10001 --gid cryptolab --create-home cryptolab

COPY --from=builder /wheels /wheels
RUN python -m pip install \
    --no-index \
    --find-links=/wheels \
    cryptolab==1.0.0 \
    && rm -rf /wheels

WORKDIR /app
RUN mkdir -p /app/data /app/backups \
    && chown -R cryptolab:cryptolab /app

USER cryptolab

ENTRYPOINT ["cryptolab"]
CMD ["paper", "--help"]
