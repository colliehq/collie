FROM node:22.22.0-bookworm-slim@sha256:dd9d21971ec4395903fa6143c2b9267d048ae01ca6d3ea96f16cb30df6187d94

ARG CLAUDE_CODE_VERSION=2.1.221

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git python3 \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && useradd --create-home --uid 10001 runner \
    && install -d -o runner -g runner /home/runner/.claude /opt/collie/bench \
    && git config --system --add safe.directory /workspace

COPY --chown=runner:runner harness /opt/collie/harness
COPY --chown=runner:runner bench/subscription_guard.py /opt/collie/bench/subscription_guard.py
COPY --chown=runner:runner bench/subscription_rank_worker.py /opt/collie/bench/subscription_rank_worker.py

ENV HOME=/home/runner \
    PYTHONPATH=/opt/collie \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER runner
WORKDIR /workspace
ENTRYPOINT ["python3", "/opt/collie/bench/subscription_rank_worker.py"]
