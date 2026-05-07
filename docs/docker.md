# Docker

`bot-cmder` ships an OCI image to GHCR alongside the PyPI publish.
Use cases: containerized SRE deployment (k8s `Deployment`,
docker-compose stack), reproducible "this is what I'm running",
zero-Python-on-host installs.

## Quick start

```bash
# Pull the latest stable
docker pull ghcr.io/zondatw/bot-cmder:latest

# One-time scaffold of operator config — drops app.yaml + .env into
# the named volume `bot-cmder-cfg`. Run interactively the first time
# so you can see the next-steps message.
docker run --rm -it \
  -v bot-cmder-cfg:/etc/bot-cmder \
  ghcr.io/zondatw/bot-cmder:latest \
  init --config-dir /etc/bot-cmder

# Edit app.yaml inside the volume — easiest way is a one-shot shell:
docker run --rm -it \
  -v bot-cmder-cfg:/etc/bot-cmder \
  --entrypoint sh \
  ghcr.io/zondatw/bot-cmder:latest \
  -c 'vi /etc/bot-cmder/app.yaml'
# (Or copy out, edit on the host, copy back. Or use a host bind mount
# instead of a named volume — see the Compose example below.)

# Enroll a TOTP user
docker run --rm -it \
  -v bot-cmder-cfg:/etc/bot-cmder \
  -v bot-cmder-state:/var/lib/bot-cmder \
  ghcr.io/zondatw/bot-cmder:latest \
  enroll-totp --user telegram:<your-id>

# Run the bot
docker run -d --name bot-cmder \
  -v bot-cmder-cfg:/etc/bot-cmder:ro \
  -v bot-cmder-state:/var/lib/bot-cmder \
  -p 47823:47823 \
  --restart unless-stopped \
  ghcr.io/zondatw/bot-cmder:latest
```

## Image tags

| Tag | What it points at | When it updates |
|---|---|---|
| `latest` | Latest stable release | Every push to `release` branch |
| `0.X.Y` | Specific release version | Pinned forever once published |
| `beta` | Latest pre-release candidate | Every push to `beta` branch |
| `main` | Latest dev cut from `main` | Every push to `main` |

Production: pin a version (`0.2.0`) for reproducibility, or `latest`
for "get the bug fixes". Don't pin to `main` or `beta` in prod —
those move under your feet.

## Volumes

The image declares two paths the operator typically mounts over:

| Mount point | Purpose | Recommended mount |
|---|---|---|
| `/etc/bot-cmder/` | Operator config — `app.yaml`, `.env` | Read-only host bind or named volume |
| `/var/lib/bot-cmder/` | Mutable state — `audit.jsonl`, `totp.sqlite` | Named volume (auto-declared `VOLUME` in the Dockerfile) |

If you forget `-v` for the state path, Docker auto-creates an
anonymous volume — your audit log + TOTP enrollments survive
container restarts but get orphaned on `docker rm`. Always use a
named volume in prod.

The image's path-resolution helpers (`bot_cmder/config/paths.py`)
honor these env vars first, so you can mount elsewhere and override:

```bash
docker run -v $HOME/my-cfg:/cfg \
           -v $HOME/my-state:/state \
           -e BOT_CMDER_CONFIG_DIR=/cfg \
           -e BOT_CMDER_STATE_DIR=/state \
           ghcr.io/zondatw/bot-cmder:latest
```

## Environment variables

The image inherits the same env-var contract as the regular `bot-cmder`
binary. All settable from `docker run -e ...`:

| Var | Purpose |
|---|---|
| `BOT_CMDER_HOST` | Bind address (image default `0.0.0.0` so `-p` works) |
| `BOT_CMDER_PORT` | Bind port (default `47823`) |
| `BOT_CMDER_CONFIG_DIR` | Where to look for `app.yaml` + `.env` (image default `/etc/bot-cmder`) |
| `BOT_CMDER_STATE_DIR` | Where audit log + TOTP store live (image default `/var/lib/bot-cmder`) |
| `BOT_CMDER_MASTER_KEY` | Fernet key for TOTP store. Usually set inside `.env`; can override via env if you prefer secrets-as-env (k8s `Secret` → env). |
| `TELEGRAM_TOKEN` / `TELEGRAM_MODE` | Telegram adapter credentials + ingestion mode |
| `DISCORD_BOT_TOKEN` / `DISCORD_PUBLIC_KEY` / `DISCORD_APPLICATION_ID` / `DISCORD_MODE` | Discord adapter |
| `SLACK_SIGNING_SECRET` / `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` / `SLACK_MODE` | Slack adapter |

See [`bot_cmder/data/app.yaml.example`](../bot_cmder/data/app.yaml.example)
for the full app.yaml reference. The image bundles this file (it ships
in the wheel); `bot-cmder init --config-dir /etc/bot-cmder` reads it.

## Healthcheck

The image's `HEALTHCHECK` polls `http://localhost:47823/healthz` every
30s. The bot returns 200 OK once startup is complete. Visible via
`docker ps`:

```
$ docker ps --format 'table {{.Names}}\t{{.Status}}'
NAMES        STATUS
bot-cmder    Up 5 minutes (healthy)
```

If you remap the bind port via `BOT_CMDER_PORT`, the healthcheck still
hits port 47823 inside the container — the port mapping (`-p`) doesn't
affect the in-container loopback. No override needed unless you also
override `BOT_CMDER_PORT`.

## docker-compose example

```yaml
services:
  bot-cmder:
    image: ghcr.io/zondatw/bot-cmder:latest
    restart: unless-stopped
    ports:
      - "47823:47823"
    volumes:
      - ./config:/etc/bot-cmder:ro
      - bot-cmder-state:/var/lib/bot-cmder
    environment:
      # Either inline (visible in `docker compose config`) or via
      # docker secrets / a referenced env_file.
      BOT_CMDER_MASTER_KEY: ${BOT_CMDER_MASTER_KEY}
      TELEGRAM_TOKEN: ${TELEGRAM_TOKEN}
      TELEGRAM_WEBHOOK_SECRET: ${TELEGRAM_WEBHOOK_SECRET}
    healthcheck:
      # Compose-level healthcheck overrides the image's; you usually
      # want the image's default. Listed here so you know it's possible.
      test: ["CMD", "curl", "-fsS", "http://localhost:47823/healthz"]
      interval: 30s
      timeout: 5s
      retries: 2

volumes:
  bot-cmder-state:
```

## Kubernetes example

Minimal `Deployment` + `Service` + `PVC` + `ConfigMap` + `Secret`:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: bot-cmder-config
data:
  app.yaml: |
    users:
      - id: zonda
        aliases: ["telegram:1234567890"]
        role: sre
    # ...
---
apiVersion: v1
kind: Secret
metadata:
  name: bot-cmder-secrets
type: Opaque
stringData:
  BOT_CMDER_MASTER_KEY: "<your-fernet-key>"
  TELEGRAM_TOKEN: "<your-telegram-token>"
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: bot-cmder-state
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: bot-cmder
spec:
  replicas: 1
  # State dir is single-writer (sqlite + jsonl append). Don't scale
  # this deployment past 1 replica until HA support lands (out of
  # scope per issue #20).
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: bot-cmder
  template:
    metadata:
      labels:
        app: bot-cmder
    spec:
      containers:
        - name: bot-cmder
          image: ghcr.io/zondatw/bot-cmder:0.2.0
          ports:
            - containerPort: 47823
          envFrom:
            - secretRef:
                name: bot-cmder-secrets
          volumeMounts:
            - name: config
              mountPath: /etc/bot-cmder
              readOnly: true
            - name: state
              mountPath: /var/lib/bot-cmder
          # Image already declares HEALTHCHECK; k8s probes are
          # explicit because k8s doesn't read OCI healthchecks.
          livenessProbe:
            httpGet: { path: /healthz, port: 47823 }
            initialDelaySeconds: 30
            periodSeconds: 30
          readinessProbe:
            httpGet: { path: /healthz, port: 47823 }
            periodSeconds: 10
      volumes:
        - name: config
          configMap:
            name: bot-cmder-config
        - name: state
          persistentVolumeClaim:
            claimName: bot-cmder-state
---
apiVersion: v1
kind: Service
metadata:
  name: bot-cmder
spec:
  type: ClusterIP
  selector:
    app: bot-cmder
  ports:
    - port: 47823
      targetPort: 47823
```

For incoming webhooks (Telegram / Discord / Slack interactions modes)
add an Ingress with TLS and the `Service` cluster-internal. For
no-domain modes (Telegram polling, Slack socket, Discord gateway),
no Ingress needed — the bot dials out, no inbound port required from
the public internet.

## Multi-arch

Image is published as a manifest list with `linux/amd64` +
`linux/arm64`. Docker auto-pulls the right arch for your host:

```bash
$ docker run --rm ghcr.io/zondatw/bot-cmder:latest --version
bot-cmder 0.2.0   # same on M-series Mac, Raspberry Pi, AWS Graviton, x86 servers
```

## Building locally

```bash
# Native arch only (fast)
docker build -t bot-cmder:local .

# Multi-arch (slow on ubuntu-latest / fast on M-series Mac with native arm64)
docker buildx build --platform linux/amd64,linux/arm64 -t bot-cmder:local .
```

The `Justfile` recipes wrap these — `just docker-build`, `just
docker-run`, `just docker-shell` for ergonomics.

## Pushing to GHCR

Maintainer-only. The release workflow handles this automatically;
manual push is documented for incident-response only:

```bash
# After bumping bot_cmder/__init__.py:__version__ on main and
# fast-forwarding the release branch
git checkout release
git merge --ff-only main
git push                # ← triggers .github/workflows/docker.yml
```

The first publish creates a private GHCR package by default. **Flip
to public once** at
https://github.com/users/zondatw/packages/container/bot-cmder/settings
(GHCR has no auto-public option for new packages — you have to click
once).

After that, anonymous `docker pull ghcr.io/zondatw/bot-cmder` works
forever without `docker login`.

## Why `slim-bookworm` and not `alpine` or distroless

- `alpine` uses musl libc; `cryptography` (Fernet, TOTP secret store)
  + `asyncssh` (the SSH connector) want glibc and ship pre-built
  manylinux wheels — alpine forces source compilation, ~5x build
  time and a much heavier image.
- `distroless` saves ~30 MB but breaks `bot-cmder init` interactivity
  and removes `curl` for the healthcheck. Trade-off not worth it for
  an SRE bot at this scale.
- `slim-bookworm` is the right balance: glibc, full CPython, ~80 MB
  base, well-maintained Debian base.

## Image size budget

Target: <150 MB per arch (compressed; `docker pull` size).

Actual breakdown (approximate, will drift over time):
- `python:3.12-slim-bookworm` base: ~50 MB
- curl + apt cleanup: ~3 MB
- venv + bot-cmder deps (FastAPI, asyncssh, pyotp, cryptography, ...): ~75 MB
- bot-cmder itself: ~0.5 MB

If the image grows past 200 MB, audit the dependencies — likely a
new transitive dep is pulling in C libs or shipping precompiled
binaries. `docker history ghcr.io/zondatw/bot-cmder:latest` shows
the per-layer size to identify the culprit.
