# Lightsmith Cloud GPU worker: one container per GPU, NVIDIA runtime via compose `deploy.resources`.
#
#   docker build -f docker/worker.Dockerfile --build-arg LIBRARY_SPEC=<spec> -t lightsmith-cloud-worker .
#
# Based on the official PyTorch CUDA runtime image; it must ship CUDA 12.8+ for RTX 50xx (Blackwell).
# The venv sees the base image's torch through --system-site-packages, so torch is not reinstalled.
ARG TORCH_IMAGE=pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime

FROM ${TORCH_IMAGE} AS build
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv==0.7.19
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
RUN uv venv --system-site-packages --python "$(command -v python)" /opt/venv
WORKDIR /src
COPY requirements/ requirements/
RUN uv pip install --python /opt/venv/bin/python -r requirements/hosted.txt
COPY wheels/ wheels/
ARG LIBRARY_SPEC
RUN test -n "$LIBRARY_SPEC" || { echo "set --build-arg LIBRARY_SPEC=<wheel path or git URL>" >&2; exit 1; } \
 && uv pip install --python /opt/venv/bin/python -c requirements/hosted.txt "$LIBRARY_SPEC"
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN uv pip install --python /opt/venv/bin/python --no-deps --no-sources . \
 && /opt/venv/bin/python -c "import torch, lightsmith_cloud.worker; print('torch', torch.__version__, 'cuda', torch.version.cuda)"

FROM ${TORCH_IMAGE} AS runtime
COPY --from=build /opt/venv /opt/venv
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app && mkdir -p /data && chown app /data
USER app
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 LIGHTSMITH_MODE=hosted LIGHTSMITH_DATA_DIR=/data
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 CMD ["lightsmith-worker", "healthcheck"]
CMD ["lightsmith-worker"]
