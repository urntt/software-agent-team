FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git=1:2.39.5-0+deb12u3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock /opt/software-agent-team/requirements.lock

RUN python -m pip install --no-cache-dir --requirement \
    /opt/software-agent-team/requirements.lock

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace

# OpenClaw creates one long-lived sandbox container per configured scope and
# executes tools into it. The Python base image would otherwise start
# `python3`, receive EOF, and exit before the first tool call.
CMD ["sleep", "infinity"]
