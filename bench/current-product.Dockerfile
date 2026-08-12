FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ARG CLAUDE_AGENT_SDK_VERSION=0.2.136
ARG CODEX_VERSION=0.147.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir "claude-agent-sdk==${CLAUDE_AGENT_SDK_VERSION}" \
    && npm install --global "@openai/codex@${CODEX_VERSION}" \
    && useradd --create-home --uid 10001 runner \
    && install -d -o runner -g runner /opt/collie/bench /home/runner/.claude \
    && git config --system --add safe.directory /workspace

COPY --chown=runner:runner harness /opt/collie/harness
COPY --chown=runner:runner bench/paired_eval.py /opt/collie/bench/paired_eval.py
COPY --chown=runner:runner bench/current_product_worker.py /opt/collie/bench/current_product_worker.py

ENV HOME=/home/runner \
    PYTHONPATH=/opt/collie \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER runner
WORKDIR /workspace
ENTRYPOINT ["python", "/opt/collie/bench/current_product_worker.py"]
