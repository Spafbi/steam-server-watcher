# syntax=docker/dockerfile:1
#
# Multi-stage Dockerfile for steam-update-check
#
# Rationale:
#   Stage 1 (steamcmd/steamcmd) already ships a fully working SteamCMD install
#   plus the 32-bit (i386) shared libraries it needs to run on an amd64 host.
#   We use it purely as a "source" image: we copy the SteamCMD install tree
#   and the i386 runtime libs out of it, then throw the rest of that image
#   away.
#
#   Stage 2 (python:3.13-slim-trixie) is the final runtime. It is small and
#   Debian-trixie based. We add only the static `docker` CLI binary we need to
#   talk to the host daemon, plus the i386 runtime libraries copied from
#   stage 1. Keeping the build image out of the final image keeps the attack
#   surface and image size small.
#
# SteamCMD layout (verified against steamcmd/steamcmd):
#   * /usr/bin/steamcmd            -> shell wrapper (sets up ~/.steam symlinks)
#   * /root/.local/share/Steam/steamcmd/  -> the real install tree:
#         steamcmd.sh              (launcher: sets LD_LIBRARY_PATH, picks linux32)
#         linux32/steamcmd         (the actual 32-bit ELF binary)
#         linux32/*.so             (steamclient.so, libtier0_s.so, ...)
#         linux64/, package/, public/, siteserverui/
#   * /lib/i386-linux-gnu/         -> the i386 runtime libs the binary links
#                                     against (libc, libstdc++, libgcc_s, ld-linux, ...)
#
# i386 lib requirement:
#   SteamCMD's real binary is a 32-bit ELF. On an amd64 Debian image it cannot
#   run unless the i386 multi-arch runtime libraries are present. Debian trixie
#   does NOT ship the old `lib32*` package names, so instead of apt we simply
#   copy the i386 libs that the steamcmd/steamcmd image already carries from
#   /lib/i386-linux-gnu. `ldd` on the binary confirms those are exactly the
#   libs it resolves against.

# ---------------------------------------------------------------------------
# Stage 1: source for SteamCMD + i386 libs
# ---------------------------------------------------------------------------
FROM steamcmd/steamcmd AS steamcmd

# Ensure the i386 lib dir exists so the COPY in stage 2 always succeeds.
RUN set -eux; \
    mkdir -p /lib/i386-linux-gnu

# ---------------------------------------------------------------------------
# Stage 2: final runtime
# ---------------------------------------------------------------------------
FROM python:3.13-slim-trixie

# --- System packages (single layer, lists cleaned in the same layer) -------
# curl + ca-certificates are needed to fetch the static docker CLI.
# bash is required by steamcmd.sh (the launcher uses `#!/usr/bin/env bash`).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        bash; \
    rm -rf /var/lib/apt/lists/*

# --- Static docker CLI (official Docker releases) --------------------------
# NOTE: the static channel uses the `x86_64` arch directory (not `amd64`).
RUN set -eux; \
    arch=x86_64; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${arch}/docker-27.5.1.tgz" -o /tmp/docker.tgz; \
    tar -xzf /tmp/docker.tgz -C /tmp; \
    install -m 0755 /tmp/docker/docker /usr/local/bin/docker; \
    rm -rf /tmp/docker /tmp/docker.tgz

# --- SteamCMD install tree + i386 libs from stage 1 ------------------------
# Copy the complete, self-contained SteamCMD install (launcher + linux32
# binary + its .so files + support dirs).
COPY --from=steamcmd /root/.local/share/Steam/steamcmd /opt/steamcmd

# i386 shared libraries the 32-bit steamcmd binary links against.
RUN set -eux; \
    mkdir -p /lib/i386-linux-gnu
COPY --from=steamcmd /lib/i386-linux-gnu/ /lib/i386-linux-gnu/

# The 32-bit ELF's interpreter is /lib/ld-linux.so.2. On amd64 Debian the i386
# loader lives at /lib/i386-linux-gnu/ld-linux.so.2, so expose it at the
# canonical 32-bit path (and the /lib32 alias) or the kernel cannot exec it.
RUN set -eux; \
    ln -sf /lib/i386-linux-gnu/ld-linux.so.2 /lib/ld-linux.so.2; \
    mkdir -p /lib32; \
    ln -sf /lib/i386-linux-gnu/ld-linux.so.2 /lib32/ld-linux.so.2

# --- steamcmd launcher on PATH --------------------------------------------
# Replicate the official wrapper's behaviour: point HOME at a writable dir so
# the ~/.steam symlinks the wrapper creates land in a predictable place, and
# expose a `steamcmd` command that runs the real launcher.
RUN set -eux; \
    printf '#!/bin/sh\nexport HOME=/home/steam\nexec /opt/steamcmd/steamcmd.sh "$@"\n' \
        > /usr/local/bin/steamcmd; \
    chmod 0755 /usr/local/bin/steamcmd

# --- Python dependencies ----------------------------------------------------
RUN set -eux; \
    pip install --no-cache-dir PyYAML==6.0.2

# --- Application ------------------------------------------------------------
WORKDIR /app
COPY steam_update_check.py /app/steam_update_check.py

# --- Environment ------------------------------------------------------------
ENV SUC_CONFIG=/config/config.yaml \
    SUC_STATE_DIR=/state \
    HOME=/home/steam \
    PYTHONUNBUFFERED=1

# --- Non-root user + permissions -------------------------------------------
# The steam user needs a writable HOME (the steamcmd wrapper creates ~/.steam
# symlinks there on first run) and ownership of the app/state/config dirs.
RUN set -eux; \
    useradd --uid 1000 --create-home --shell /usr/sbin/nologin steam; \
    mkdir -p /state /config; \
    chown -R steam:steam /app /state /config /home/steam

USER steam

VOLUME ["/state", "/config"]

# --- Optional healthcheck ---------------------------------------------------
# Runs a single check pass against the configured config. Safe to remove if
# you prefer to drive liveness/readiness from an orchestrator instead.
HEALTHCHECK --interval=30s --timeout=25s --start-period=60s --retries=3 \
    CMD python /app/steam_update_check.py --once --config /config/config.yaml

ENTRYPOINT ["python", "/app/steam_update_check.py"]
CMD ["--verbose"]
