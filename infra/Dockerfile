FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

FROM python:3.12-slim-trixie AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY core ./core
COPY apps ./apps
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-trixie
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin af
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 10001
