# Lightsmith Cloud API image (CPU, with CPU-only PyTorch). Also used for `migrate` and the CPU-only worker.
#
#   docker build -f docker/api.Dockerfile --build-arg LIBRARY_SPEC=<spec> -t lightsmith-cloud-api .
#
# The build context is this directory (the lightsmith-cloud repo root). LIBRARY_SPEC is any pip
# requirement for the scientific library until it is published, e.g.
#   wheels/lightsmith-0.1.0-py3-none-any.whl                      (a wheel copied into ./wheels)
#   "lightsmith @ git+https://github.com/<org>/<repo>@<tag>"     (a git URL)
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv==0.7.19
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
RUN uv venv /opt/venv
WORKDIR /src
# CPU-only PyTorch, the same version as the worker image, so backends that need torch (inverse design,
# GPU-capable solvers) are "available" here too: validation and estimate() must agree with the workers.
ARG TORCH_VERSION=2.7.1
RUN uv pip install --python /opt/venv/bin/python --index-url https://download.pytorch.org/whl/cpu       "torch==${TORCH_VERSION}"
COPY requirements/ requirements/
RUN uv pip install --python /opt/venv/bin/python -r requirements/hosted.txt
COPY wheels/ wheels/
ARG LIBRARY_SPEC
RUN test -n "$LIBRARY_SPEC" || { echo "set --build-arg LIBRARY_SPEC=<wheel path or git URL>" >&2; exit 1; } \
 && uv pip install --python /opt/venv/bin/python -c requirements/hosted.txt "$LIBRARY_SPEC"
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN uv pip install --python /opt/venv/bin/python --no-deps --no-sources . \
 && /opt/venv/bin/python -c "import lightsmith_cloud.app, lightsmith_cloud.lib as l; print(l.osc.__name__, l.osc.__version__)"

FROM python:${PYTHON_VERSION}-slim AS runtime
COPY --from=build /opt/venv /opt/venv
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app && mkdir -p /data && chown app /data
USER app
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 \
    LIGHTSMITH_MODE=hosted LIGHTSMITH_DATA_DIR=/data LIGHTSMITH_HOST=0.0.0.0
EXPOSE 8200
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8200/v1/health', timeout=4)"
CMD ["lightsmith-server"]
