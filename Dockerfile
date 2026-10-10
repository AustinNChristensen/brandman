# Build the wheel and dashboard with Node; the runtime only needs Python.
FROM python:3.12-slim-bookworm AS build
COPY --from=node:22-bookworm-slim /usr/local/ /usr/local/
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /bin/uv
ENV UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/brandman
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE hatch_build.py ./
RUN uv sync --locked --no-dev --no-install-project
COPY brandman ./brandman
COPY web ./web
COPY scripts ./scripts
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH=/opt/brandman/bin:$PATH \
    BRANDMAN_DB=/data/brandman.db BRANDMAN_DATABASE_PROFILE=operating
COPY --from=build /opt/brandman /opt/brandman
RUN useradd --system --uid 10001 --home-dir /data brandman \
    && mkdir -p /data && chown brandman /data
USER brandman
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)" || exit 1
# Configure FORWARDED_ALLOW_IPS to the trusted TLS proxy; never use '*'.
CMD ["uvicorn", "brandman.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
