FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git=1:2.39.5-0+deb12u3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock /opt/software-agent-team/requirements.lock
COPY uv-offline.toml /opt/software-agent-team/uv-offline.toml
COPY warm_public_uv_cache.py /tmp/warm_public_uv_cache.py

RUN python -m pip install --no-cache-dir --requirement \
    /opt/software-agent-team/requirements.lock

RUN python /tmp/warm_public_uv_cache.py \
      --requirements /opt/software-agent-team/requirements.lock \
      --project /tmp/sat-runtime-catalog \
    && UV_CACHE_DIR=/opt/software-agent-team/uv-public-cache \
      UV_CONFIG_FILE=/dev/null \
      UV_DEFAULT_INDEX=https://pypi.org/simple \
      uv lock --project /tmp/sat-runtime-catalog \
    && rm -rf /tmp/sat-runtime-catalog /tmp/warm_public_uv_cache.py \
    && chmod -R a=rX /opt/software-agent-team/uv-public-cache

RUN mkdir -p /opt/software-agent-team/wheels \
    && python -m pip download --no-cache-dir --only-binary=:all: \
    --dest /opt/software-agent-team/wheels --requirement \
    /opt/software-agent-team/requirements.lock colorama==0.4.6 \
    && chmod -R a=rX /opt/software-agent-team/wheels \
    && chown 0:0 /opt/software-agent-team/uv-offline.toml \
    && chmod 0444 /opt/software-agent-team/uv-offline.toml

COPY sat_probe_write.py /usr/local/bin/sat-probe-write
COPY sat_probe_run.py /usr/local/bin/sat-probe-run
COPY sat_project_lock.py /usr/local/bin/sat-project-lock

RUN chown 0:0 /usr/local/bin/sat-probe-write /usr/local/bin/sat-probe-run \
      /usr/local/bin/sat-project-lock \
    && chmod 0555 /usr/local/bin/sat-probe-write /usr/local/bin/sat-probe-run \
      /usr/local/bin/sat-project-lock

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_CONFIG_FILE=/opt/software-agent-team/uv-offline.toml

WORKDIR /workspace

# Keep direct image runs useful for lifecycle diagnostics. OpenClaw supplies
# this same supervisor command explicitly when it creates a role sandbox.
CMD ["sleep", "infinity"]
