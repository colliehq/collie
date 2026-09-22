FROM node@sha256:9f6d5975c7dca860947d3915877f85607946403fc55349f39b4bc3688448bb6e

ARG PI_VERSION=0.84.1
ARG HERMES_VERSION=0.15.2
ARG PRIME_COMMIT=0987c1ba7637cbcb99afe9efe1180b838a0aa958

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates git python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && git clone --filter=blob:none https://github.com/PrimeIntellect-ai/prime-agent.git /opt/prime-agent \
    && git -C /opt/prime-agent checkout --detach "${PRIME_COMMIT}" \
    && cd /opt/prime-agent \
    && npm ci \
    && npm run build \
    && python3 -m venv /opt/hermes-venv \
    && /opt/hermes-venv/bin/python -m pip install --no-cache-dir "hermes-agent==${HERMES_VERSION}" \
    && python3 -m venv /opt/prime-kernel \
    && /opt/prime-kernel/bin/python -m pip install --no-cache-dir \
       /opt/prime-agent/prime-agent-runtime \
       beautifulsoup4 dill httpx ipykernel lxml numpy pandas pydantic \
       python-dotenv pyyaml requests scipy tomli tyro \
    && useradd --create-home --uid 10001 runner \
    && install -d -o runner -g runner /opt/collie/bench /opt/collie/harness /state \
    && git config --system --add safe.directory /workspace \
    && git config --system --add safe.directory /opt/prime-agent

COPY --chown=runner:runner harness /opt/collie/harness
COPY --chown=runner:runner bench/normalized_harness_worker.py /opt/collie/bench/normalized_harness_worker.py
COPY --chown=runner:runner bench/normalized_prime_pi.py /opt/collie/bench/normalized_prime_pi.py
COPY --chown=runner:runner bench/normalized_hermes.py /opt/collie/bench/normalized_hermes.py

ENV HOME=/home/runner \
    PYTHONPATH=/opt/collie \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRIME_AGENT_KERNEL_PYTHON=/opt/prime-kernel/bin/python \
    PATH=/opt/hermes-venv/bin:/opt/prime-kernel/bin:/usr/local/bin:/usr/bin:/bin

USER runner
WORKDIR /workspace
ENTRYPOINT ["python3", "/opt/collie/bench/normalized_harness_worker.py"]
