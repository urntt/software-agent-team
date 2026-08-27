FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git=1:2.39.5-0+deb12u3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock /opt/software-agent-team/requirements.lock

RUN python -m pip install --no-cache-dir --requirement \
    /opt/software-agent-team/requirements.lock

RUN mkdir -p /opt/software-agent-team/wheels \
    && python -m pip download --no-cache-dir --only-binary=:all: \
    --dest /opt/software-agent-team/wheels --requirement \
    /opt/software-agent-team/requirements.lock colorama==0.4.6 \
    && chmod -R a=rX /opt/software-agent-team/wheels

COPY sat_probe_write.py /usr/local/bin/sat-probe-write

RUN chown 0:0 /usr/local/bin/sat-probe-write \
    && chmod 0555 /usr/local/bin/sat-probe-write

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_FIND_LINKS=/opt/software-agent-team/wheels \
    UV_OFFLINE=1

WORKDIR /workspace

# Keep direct image runs useful for lifecycle diagnostics. OpenClaw supplies
# this same supervisor command explicitly when it creates a role sandbox.
CMD ["sleep", "infinity"]
