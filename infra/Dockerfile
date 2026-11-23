FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS deps
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The embedding model (ADR 0004), downloaded at the revision pinned in
# integrations/embeddings/models.py. Only that package is copied, so this layer is rebuilt when
# the model or its loader changes, not on every code change.
FROM deps AS models
COPY integrations/__init__.py ./integrations/__init__.py
COPY integrations/embeddings ./integrations/embeddings
RUN /opt/venv/bin/python -m integrations.embeddings.fetch download /opt/af/models

FROM deps AS build
COPY README.md LICENSE ./
COPY core ./core
COPY apps ./apps
COPY db ./db
COPY integrations ./integrations
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin af
COPY --from=build /opt/venv /opt/venv
COPY --from=models /opt/af/models /opt/af/models
# HF_HUB_OFFLINE: the model is in the image and nothing may be downloaded at run time.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AF_EMBEDDING_MODELS_DIR=/opt/af/models \
    HF_HUB_OFFLINE=1
USER 10001
# Fails the build unless the run-time user can load the model without the network.
RUN python -m integrations.embeddings.fetch check /opt/af/models
