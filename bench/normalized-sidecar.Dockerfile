FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ARG CLAUDE_AGENT_SDK_VERSION=0.2.136

RUN python -m pip install --no-cache-dir "claude-agent-sdk==${CLAUDE_AGENT_SDK_VERSION}" \
    && useradd --create-home --uid 10001 runner \
    && install -d -o runner -g runner /opt/collie/harness /ledger /home/runner/.claude

COPY --chown=runner:runner harness /opt/collie/harness

ENV HOME=/home/runner \
    PYTHONPATH=/opt/collie \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER runner
EXPOSE 8765
ENTRYPOINT ["python", "-m", "harness.subscription_sidecar"]
