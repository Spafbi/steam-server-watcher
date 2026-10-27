#!/usr/bin/env bash
# setup.sh — one-time setup for steam-server-watcher
# Detects the host's docker group GID and writes a .env file that
# docker-compose.yml picks up automatically.

set -euo pipefail

# --- Pre-flight checks -------------------------------------------------------
if ! command -v docker &>/dev/null; then
    echo "ERROR: Docker is not installed or not in PATH." >&2
    echo "       Install Docker first: https://docs.docker.com/get-docker/" >&2
    exit 1
fi

if ! docker info &>/dev/null 2>&1; then
    echo "WARNING: Cannot reach the Docker daemon. Are you in the 'docker' group?" >&2
    echo "         Run: sudo usermod -aG docker $USER && newgrp docker" >&2
    exit 1
fi

# --- Detect docker group GID -------------------------------------------------
if command -v getent &>/dev/null; then
    GID=$(getent group docker | cut -d: -f3)
else
    # Fallback: read from the socket's group
    GID=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || stat -f '%g' /var/run/docker.sock)
fi

if [[ -z "$GID" || "$GID" == "0" ]]; then
    echo "ERROR: Could not determine the docker group GID." >&2
    echo "       Set it manually:  echo 'DOCKER_GID=<gid>' > .env" >&2
    exit 1
fi

# --- Write .env ---------------------------------------------------------------
echo "DOCKER_GID=$GID" > .env
echo ""
echo "✓ Wrote .env with DOCKER_GID=$GID"
echo ""
echo "Next steps:"
echo "  1. Edit config.yaml  (set your app IDs & container names)"
echo "  2. docker compose up -d"
echo "  3. docker compose logs -f steam-server-watcher"
