# Kai — container image.
#
# This file is NOT part of how Kai works. It is one more consumer of the same
# packaging every other install path uses: it pip-installs the project and sets
# the same KAI_* environment variables a person would set by hand. Nothing in
# kai/ imports, detects, or branches on Docker, and deleting this directory
# costs the project nothing.
#
# Ollama deliberately stays OUTSIDE this image. Kai needs a GPU to be useful, and
# putting Ollama in here would drag ROCm/CUDA device passthrough into the build —
# at which point the container stops being optional and becomes the only place
# Kai is known to run. Point KAI_OLLAMA_URL at an Ollama you already have.
#
# Build from the repo root (the build context is the project, not this folder):
#   docker build -f docker/Dockerfile -t kai:0.7.0 .

FROM python:3.12-slim AS build

WORKDIR /src
RUN pip install --no-cache-dir build

# Copy only what the wheel build reads, so edits to docs/tests don't bust the layer.
COPY pyproject.toml README.md LICENSE ./
COPY kai/ ./kai/
RUN python -m build --wheel -o /dist


FROM python:3.12-slim

# Stamped by scripts/release.sh so a pulled image can say what it is.
ARG KAI_VERSION=dev

# org.opencontainers.image.source is what links this package to the GitHub repo
# on GHCR — without it the package page is orphaned and inherits no permissions.
LABEL org.opencontainers.image.source="https://github.com/supasoulja/newb_agent" \
      org.opencontainers.image.description="Kai — a local AI agent. Python + Ollama + SQLite, no cloud calls." \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${KAI_VERSION}"

# ffmpeg: audio decode for the voice features. curl: the HEALTHCHECK below.
# Everything else Kai needs is a pure-Python wheel.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*

# [voice,documents] but not [browser]: Playwright also needs ~400 MB of browser
# binaries and a pile of system libs. Add it in your own layer if you want it.
# The extras have to attach to the resolved filename: `/tmp/*.whl[voice,documents]`
# is one glob pattern to the shell (the brackets read as a character class), so it
# never expands and pip receives the literal string. Resolve it first.
COPY --from=build /dist/ /tmp/dist/
RUN WHEEL="$(ls /tmp/dist/*.whl)" \
 && pip install --no-cache-dir "${WHEEL}[voice,documents]" \
 && rm -rf /tmp/dist

# Kai writes to its data dir and its workspace; it never needs to write anywhere
# else, so it does not run as root.
#
# /data must exist and be owned by kai BEFORE the VOLUME declaration: a named
# volume inherits ownership from the image path it covers, and if the directory
# is absent Docker creates it root-owned and the app cannot write to its own DB.
# (A bind mount keeps the host's ownership instead — chown it yourself, or run
# with --user "$(id -u):$(id -g)".)
RUN useradd --create-home --uid 1000 kai \
 && mkdir -p /data \
 && chown -R kai:kai /data
USER kai

# ── The entire container-specific configuration ───────────────────────────────
# Five env vars, every one of which exists for native reasons too. That is the
# test for whether Docker is load-bearing: if the image needed a setting no
# other install path could use, the code would have grown a Docker-shaped hole.
#
# host.docker.internal resolves on Docker Desktop and, on Linux, via the
# --add-host flag that docker/compose.yaml sets.
ENV KAI_VAR_DIR=/data \
    KAI_MODELS_DIR=/data/models \
    KAI_SKILLS_DIR=/data/user_skills \
    KAI_WORKSPACE_DIR=/data/KaiFiles \
    KAI_OLLAMA_URL=http://host.docker.internal:11434 \
    PYTHONUNBUFFERED=1

# Not Kai settings — standard cache vars, pointed at the volume so the Whisper
# model (~462 MB, fetched from HuggingFace on first voice use) survives a
# container recreate instead of being re-downloaded into a dead layer.
ENV HF_HOME=/data/cache/huggingface \
    XDG_CACHE_HOME=/data/cache

VOLUME ["/data"]
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7860/ || exit 1

# --host 0.0.0.0 because the container's loopback is not reachable from outside
# it; --no-browser because there is no browser in here to open.
ENTRYPOINT ["kai-web"]
CMD ["--host", "0.0.0.0", "--port", "7860", "--no-browser"]
