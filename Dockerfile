# Both base images are pinned by digest (manifest-list digest, looked up from registry metadata,
# never a mutable tag). Dependabot (.github/dependabot.yml) opens a PR when either digest moves;
# see docs/deployment-guide.md for the manual refresh steps if one needs updating sooner.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY artio ./artio

FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
ARG ARTIO_VERSION=dev
ENV PATH=/app/.venv/bin:$PATH HOME=/tmp PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 ARTIO_VERSION=$ARTIO_VERSION
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin artio
WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY --from=build /app/artio ./artio
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "--factory", "artio.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
