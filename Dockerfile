# Multi-stage build for bot-cmder (issue #25).
#
# Stage 1 builds the wheel from the repo source. Stage 2 installs that
# wheel into a venv on a fresh slim base, dropping every build tool —
# the runtime image only carries Python + the installed package + its
# transitive deps.
#
# Target final image size: ~130-150 MB per arch.
# Built for: linux/amd64, linux/arm64.

# --- Stage 1: build wheel ----------------------------------------------------

FROM python:3.12-slim-bookworm AS builder

# uv is the single build tool; everything else is its job to manage.
RUN pip install --no-cache-dir uv

WORKDIR /build

# Copy only the files needed to build the wheel. .dockerignore drops
# the rest of the repo (var/, .claude/, .git/, etc.) so this COPY
# stays minimal and the layer cache holds across normal source edits.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY bot_cmder/ ./bot_cmder/

# `uv build --wheel` produces dist/*.whl. We don't need the sdist in
# the runtime stage; --wheel skips its generation and saves a few seconds.
RUN uv build --wheel

# --- Stage 2: minimal runtime ------------------------------------------------

FROM python:3.12-slim-bookworm

# OCI metadata — the source / description / license labels show up
# under the "About" section of the GHCR package page. The source label
# in particular wires up "Connected repository" so GHCR auto-links the
# package to the repo without a separate UI step.
LABEL org.opencontainers.image.source="https://github.com/zondatw/bot-cmder"
LABEL org.opencontainers.image.description="Multi-platform SRE ChatOps bot — drive ops via Telegram / Discord / Slack"
LABEL org.opencontainers.image.licenses="MIT"

# Add curl for the HEALTHCHECK below. slim-bookworm omits it; ~3 MB
# extra weight buys a real liveness probe via the existing /healthz
# endpoint that exists for exactly this purpose.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Non-root runtime user — UID 1000. Keys / config files mounted at
# runtime should be readable by this UID. No login shell, no home
# directory; this user only ever runs `bot-cmder`.
RUN groupadd --system --gid 1000 botcmder && \
    useradd --system --uid 1000 --gid botcmder --no-create-home --shell /usr/sbin/nologin botcmder

# Install the wheel into a venv. Keeping bot-cmder out of system Python
# means a future `apt-get install python3-something` can't accidentally
# clobber a transitive dep, and `pip install --upgrade` inside the
# container is sandboxed.
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
RUN python -m venv $VIRTUAL_ENV
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# XDG-style paths inside the image. The bot's path-resolution helpers
# (bot_cmder/config/paths.py) honor these env vars first, so an
# operator who mounts elsewhere just overrides the env var at
# `docker run` time:
#
#   docker run -v ./my-config:/opt/cfg \
#              -e BOT_CMDER_CONFIG_DIR=/opt/cfg \
#              ghcr.io/zondatw/bot-cmder
#
# Defaults below are the conventional FHS locations so SREs don't have
# to think.
ENV BOT_CMDER_CONFIG_DIR=/etc/bot-cmder
ENV BOT_CMDER_STATE_DIR=/var/lib/bot-cmder

# Pre-create both directories with the right owner. The state dir is
# also declared as a VOLUME below so Docker auto-creates an anonymous
# volume if the user forgets `-v`, preventing accidental writes into
# the writable layer (lost on `docker rm`).
RUN mkdir -p /etc/bot-cmder /var/lib/bot-cmder && \
    chown -R botcmder:botcmder /etc/bot-cmder /var/lib/bot-cmder
VOLUME ["/var/lib/bot-cmder"]

USER botcmder
EXPOSE 47823

# Healthcheck against the bot's own /healthz endpoint. Returns 200 OK
# when the FastAPI app is responsive. 30s start period covers the
# adapter / SSH-pool warm-up; after that, two consecutive failures
# (60s) flips the container to unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=2 \
    CMD curl -fsS http://localhost:47823/healthz || exit 1

# `bot-cmder` is the entrypoint; the subcommand passes through CMD.
# Default is `serve --host 0.0.0.0` so `docker run -p 47823:47823 ...`
# Just Works — without 0.0.0.0 the bot binds to the container's
# loopback only and `-p` can't reach it. Override at runtime for any
# other CLI surface:
#
#   docker run --rm ghcr.io/zondatw/bot-cmder --version
#   docker run --rm -v ./cfg:/etc/bot-cmder ghcr.io/zondatw/bot-cmder init --config-dir /etc/bot-cmder
ENTRYPOINT ["bot-cmder"]
CMD ["serve", "--host", "0.0.0.0"]
