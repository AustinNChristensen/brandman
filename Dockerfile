# BrandMan: dashboard, REST API and hosted MCP endpoint in one image.
# The database lives on the /data volume; never bake it into the image.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/brandman

WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY brandman ./brandman
RUN uv sync --locked --no-dev --no-editable \
    && useradd --system --uid 10001 --home-dir /data brandman \
    && mkdir -p /data && chown brandman /data

ENV PATH=/opt/brandman/bin:$PATH \
    BRANDMAN_DB=/data/brandman.db \
    BRANDMAN_DATABASE_PROFILE=operating
USER brandman
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8000

# The public home page is the only unauthenticated route.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)" || exit 1

# --proxy-headers reads X-Forwarded-Proto/For only from addresses listed in
# FORWARDED_ALLOW_IPS (uvicorn's default is 127.0.0.1). Set it to your TLS
# proxy's address; never "*", which would let any client claim to be local.
CMD ["uvicorn", "brandman.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
