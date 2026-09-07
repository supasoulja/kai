# Running Kai in a container

**Optional.** Kai's supported install is a clone or `pip install kai-agent`. This
directory is a convenience wrapper, and deleting it costs the project nothing:

```bash
rm -rf docker/ .dockerignore   # nothing else in the repo changes
```

## The rule this layer follows

> Docker is a **consumer** of Kai's packaging, not Kai's packaging.

Concretely:

- Nothing under `kai/` imports, detects, or branches on being containerised.
  `tests/test_packaging.py::test_no_container_detection` fails the build if that
  ever changes.
- The image installs the wheel — the same artifact `pip install kai-agent`
  installs. It does not `COPY . /app` and run scripts out of a checkout, because
  that would bake the repo layout into the image and make the image the only
  place Kai is known to work.
- The container's entire configuration is five environment variables, and every
  one of them exists for a native reason. `KAI_OLLAMA_URL` is for pointing Kai at
  a GPU box on your LAN; `KAI_VAR_DIR` is for putting your data on another disk.
  The container just uses them too.

That last point is the actual test. If the image ever needs a setting no other
install path can use, a Docker-shaped hole has been cut into the app — fix the
packaging rather than widening the hole.

## Ollama stays outside

There is no `ollama` service in `compose.yaml`, deliberately. Kai needs a GPU to
be useful, and putting Ollama in the stack drags ROCm/CUDA device passthrough
into the compose file. At that point `docker compose up` becomes the only way
anyone can run Kai, which is precisely the dependency this layout avoids.

Point Kai at an Ollama you already run:

| Where Ollama runs | `KAI_OLLAMA_URL` |
|---|---|
| The Docker host | `http://host.docker.internal:11434` (default) |
| Another machine on the LAN | `http://192.168.1.50:11434` |
| Its own container | `http://ollama:11434` (add the service yourself) |

On Linux, `host.docker.internal` only resolves because `compose.yaml` sets
`extra_hosts: host-gateway`. With plain `docker run`, pass
`--add-host=host.docker.internal:host-gateway`.

Ollama also has to listen somewhere the container can reach it — by default it
binds loopback only, which the container cannot see:

```bash
OLLAMA_HOST=0.0.0.0 ollama serve
```

## Usage

```bash
# From the repo root — the build context is the project, not this folder.
docker build -f docker/Dockerfile -t kai:0.7.0 .

docker run -d --name kai \
  -p 127.0.0.1:7860:7860 \
  -v kai-data:/data \
  --add-host=host.docker.internal:host-gateway \
  kai:0.7.0
```

Or with compose:

```bash
docker compose -f docker/compose.yaml up --build
```

Then open <http://127.0.0.1:7860>.

## What's in the image, and what isn't

Included: core + `[voice,documents]`, plus `ffmpeg` for audio decode.

Not included, on purpose:

- **`[browser]`** (Playwright) — another ~400 MB of browser binaries and system
  libs. Add it in your own layer if you need `browser.*` tools.
- **The desktop app** (`kai-app`) — it opens a native GTK/WebKit window and a
  tray icon. Containerising a desktop app means X11 or Wayland socket
  forwarding; run it on the host instead.
- **Ollama and the model weights** — see above. Models live in your host's
  Ollama, not in this image.

## Updating

Pull the new image and recreate the container. Your data volume is untouched.

```bash
docker compose -f docker/compose.yaml pull
docker compose -f docker/compose.yaml up -d
```

Kai narrates what changed on the first start after an upgrade (it compares the
stored version against `changelog.json`). To pin or roll back, name the tag
explicitly: `ghcr.io/supasoulja/kai:0.7.0`.

Releases are cut with `scripts/release.sh` — see [docs/active/RELEASE.md] for the
full flow, including the volume-backup step to take before a schema change.

## Data

Everything mutable is under `/data`, mapped to one named volume:

```
/data/memory/      SQLite DBs (kai.db, events.db + WAL sidecars)
/data/knowledge/   per-user knowledge stores
/data/state/       runtime state
/data/tree/        tree memory
/data/tls/         self-signed certs
/data/models/      ONNX embed + Kokoro voice binaries (downloaded on first run)
/data/user_skills/ your SKILL.md recipes
/data/KaiFiles/    Kai's file-write sandbox, including notes/
/data/cache/       HuggingFace cache (the Whisper STT model lands here)
```

Back it up by backing up that volume. The image holds no state.

## The port binding

`compose.yaml` publishes to `127.0.0.1:7860`, not `0.0.0.0:7860`. Kai's auth is
session-cookie based and built for a machine you control — it is not hardened for
the open internet. If you want it on your LAN, use the app's own TLS path
(`kai-web --lan`) and understand what you are exposing.
