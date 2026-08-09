FROM python:3.12.13-slim-bookworm

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git=1:2.39.5-0+deb12u2 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir \
        fastapi==0.116.1 \
        httpx==0.28.1 \
        jinja2==3.1.6 \
        pytest==8.4.2 \
        python-multipart==0.0.20 \
        ruff==0.12.8 \
        uvicorn==0.35.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace
