# syntax=docker/dockerfile:1
#
# agent2kb backend image: the `a2kb` CLI, the claude/codex CLIs (optional) and
# supercronic for scheduled compile / lint / backup, and the one-shot bootstrap and MCP
# commands the compose file runs. See docs/docker.md.
#
# Build args:
#   KB_EXTRAS     pip extras to install (default: all, which leaves out the heavy
#                 local-transcription extra). Try "media,mcp" for a smaller image,
#                 or "all,whisper" to transcribe audio and video inside the image.
#   INSTALL_CLIS  1 = install the claude and codex CLIs (needed for
#                 compile_provider: cli), 0 = skip them.

# ── Stage 1: claude / codex CLIs (npm) ──────────────────────────────────
# Base images are pinned by digest (tag@sha256). To refresh: look up the tag's
# current digest on the registry and replace it here.
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS clis
ARG INSTALL_CLIS=1
ARG CLAUDE_CODE_VERSION=2.1.302
ARG CODEX_VERSION=0.162.1
# The two packages are pinned to exact versions, but there is no lockfile: their own
# dependencies resolve at build time, so two builds of the same tag can differ.
RUN mkdir -p /opt/clis/bin \
    && if [ "$INSTALL_CLIS" = "1" ]; then \
         npm install -g --prefix /opt/clis --no-fund --no-audit \
           "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
         && cp /usr/local/bin/node /opt/clis/bin/node; \
       fi

# ── Stage 2: runtime ────────────────────────────────────────────────────
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ARG KB_EXTRAS=all
ARG SUPERCRONIC_VERSION=v0.2.33
ARG SUPERCRONIC_SHA256_AMD64=feefa310da569c81b99e1027b86b27b51e6ee9ab647747b49099645120cfc671
ARG SUPERCRONIC_SHA256_ARM64=f1f8585c66de020fef494dd636058f99949d108f569fef00016a1c8b9eb145b3
ARG TARGETARCH

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         ca-certificates curl ffmpeg pandoc libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# supercronic: cron for containers. Pinned by version and verified by checksum.
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) sha="$SUPERCRONIC_SHA256_AMD64" ;; \
      arm64) sha="$SUPERCRONIC_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH:-amd64}"; \
    echo "${sha}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/supercronic

# The claude/codex CLIs run on the same node binary they were installed with
# (copied from the node stage, so versions cannot drift). That binary links only
# glibc, libstdc++ and libgcc_s; glibc comes from this image (same or newer than
# the node stage's), and libstdc++6 is installed explicitly above rather than
# assumed. Installing Debian's nodejs here instead would pair the CLIs with a
# different node major than they were installed under.
COPY --from=clis /opt/clis /opt/clis
ENV PATH="/opt/clis/bin:${PATH}"

# agent2kb
WORKDIR /opt/kb
COPY pyproject.toml README.md LICENSE ./
COPY agent2kb ./agent2kb
RUN pip install ".[${KB_EXTRAS}]"
COPY scripts ./scripts
COPY docker/entrypoint.sh /usr/local/bin/kb-entrypoint
RUN chmod 0755 /usr/local/bin/kb-entrypoint /opt/kb/scripts/*.sh

# Non-root user. The directories below are empty here: compose keeps each one in a named
# volume, which starts with this ownership (so nothing needs a chown on the host).
#   /data     per-KB logs and local backups
#   /secrets  keys.json, the master copy of every generated key incl. the S3 admin key
#             (`a2kb bootstrap init`; mounted only by kb-init and kb-provision)
#   /s3conf   s3.json for SeaweedFS            /connect  Obsidian keys + MCP tokens for the kb service
#   /mcpkeys  MCP ports + tokens for kb-mcp    /proxy    generated Caddyfile
# The claude and codex logins and the kb config live under /home/kb, also a volume.
RUN useradd --create-home --uid 1000 --shell /bin/bash kb \
    && mkdir -p /data /secrets /s3conf /connect /mcpkeys /proxy \
    && chown -R kb:kb /data /secrets /s3conf /connect /mcpkeys /proxy /home/kb \
    && chmod 0700 /secrets /s3conf /connect /mcpkeys
USER kb
WORKDIR /data
ENV HOME=/home/kb \
    USER=kb \
    KB_SECRETS_DIR=/secrets \
    KB_S3CONF_DIR=/s3conf \
    KB_CONNECT_DIR=/connect \
    KB_MCPKEYS_DIR=/mcpkeys \
    KB_PROXY_DIR=/proxy

ENTRYPOINT ["kb-entrypoint"]
CMD ["cron"]
