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
#   docker build -f docker/Dockerfile -t kai:dev .
#
# Nothing environment-specific is baked in — no IPs, host paths, or secrets.
# What varies between machines is a build arg or a runtime env var:
#
#   build arg        default                              what it changes
#   PYTHON_VERSION   3.12                                 base image tag
#   KAI_VERSION      dev                                  version label (release.sh stamps it)
#   KAI_EXTRAS       voice,documents,browser              pip extras; "" installs core only
#   KAI_UID/KAI_GID  1000/1000                            owner of /data — match your host
#                                                         user when bind-mounting
#   KAI_SOURCE_URL   https://github.com/supasoulja/kai    repo the GHCR package links to
#
#   runtime env      KAI_OLLAMA_URL (docker run -e ...)   where your Ollama listens

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build

WORKDIR /src
RUN pip install --no-cache-dir build

# Copy only what the wheel build reads, so edits to docs/tests don't bust the layer.
# .dockerignore is an allowlist of exactly these, so nothing else can reach a layer.
COPY pyproject.toml README.md LICENSE ./
COPY kai/ ./kai/
RUN python -m build --wheel -o /dist


FROM python:${PYTHON_VERSION}-slim

ARG KAI_VERSION=dev
ARG KAI_EXTRAS=voice,documents,browser
ARG KAI_UID=1000
ARG KAI_GID=1000
ARG KAI_SOURCE_URL=https://github.com/supasoulja/kai

# org.opencontainers.image.source is what links this package to the GitHub repo
# on GHCR — without it the package page is orphaned and inherits no permissions.
LABEL org.opencontainers.image.source="${KAI_SOURCE_URL}" \
      org.opencontainers.image.description="Kai — a local-first AI agent. Python + Ollama + SQLite; cloud brains are opt-in." \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${KAI_VERSION}"

# ffmpeg: audio decode for the voice features. curl: the HEALTHCHECK below.
# Everything else Kai needs is a pure-Python wheel.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*

# The extras have to attach to the resolved filename: `/tmp/*.whl[voice,documents]`
# is one glob pattern to the shell (the brackets read as a character class), so it
# never expands and pip receives the literal string. Resolve it first. An empty
# KAI_EXTRAS drops the brackets entirely.
#
# [browser] is only the Playwright package. When it is in KAI_EXTRAS, Chromium's
# headless shell and the system libraries it needs are installed too, so the
# browser.* and design.render tools work in the container. It has to happen here,
# as root, before USER kai.
#
# PLAYWRIGHT_BROWSERS_PATH is Playwright's own variable, not a Kai setting. Without
# it Playwright looks under XDG_CACHE_HOME, which points at the /data volume at run
# time, and would never find the browser installed into the image.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
COPY --from=build /dist/ /tmp/dist/
RUN WHEEL="$(ls /tmp/dist/*.whl)" \
 && pip install --no-cache-dir "${WHEEL}${KAI_EXTRAS:+[${KAI_EXTRAS}]}" \
 && case ",${KAI_EXTRAS}," in \
      *,browser,*) python -m playwright install --with-deps --only-shell chromium \
                   && rm -rf /var/lib/apt/lists/* ;; \
    esac \
 && rm -rf /tmp/dist

# Kai writes to its data dir and its workspace; it never needs to write anywhere
# else, so it does not run as root.
#
# /data must exist and be owned by kai BEFORE the VOLUME declaration: a named
# volume inherits ownership from the image path it covers, and if the directory
# is absent Docker creates it root-owned and the app cannot write to its own DB.
# (A bind mount keeps the host's ownership instead — build with KAI_UID/KAI_GID
# set to your host user, or run with --user "$(id -u):$(id -g)".)
RUN groupadd --gid "${KAI_GID}" kai \
 && useradd --uid "${KAI_UID}" --gid kai --create-home kai \
 && mkdir -p /data \
 && chown -R kai:kai /data
USER kai

# ── The entire container-specific configuration ───────────────────────────────
# Five env vars, every one of which exists for native reasons too. That is the
# test for whether Docker is load-bearing: if the image needed a setting no
# other install path could use, the code would have grown a Docker-shaped hole.
#
# host.docker.internal resolves on Docker Desktop and, on Linux, via the
# --add-host flag that docker/compose.yaml sets. Override at run time with
# `-e KAI_OLLAMA_URL=http://<your-ollama-host>:11434`.
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
