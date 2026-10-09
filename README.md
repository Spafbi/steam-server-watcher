# steam-server-watcher

A containerized service that watches a list of Steam dedicated-server App IDs
for build updates (via SteamCMD) and restarts the associated Docker containers
whenever a new build is detected.

## How it works

1. Loads a YAML config listing the apps to watch, their target branch, and the
   containers to restart on update.
2. Every polling interval it runs a **single** steamcmd session that prints all
   configured apps at once —
   `steamcmd +login anonymous +app_info_print <id1> +app_info_print <id2> ... +quit` —
   then parses each app's per-branch `buildid` from its own block (anchored on
   the `AppID : <id>` header) under `depots → <depot> → branches → <branch>`.
   Batching all apps into one Steam connection is faster and more reliable than
   one process per app. If any app is missing (e.g. a transient Steam
   connection failure, `rc=254`), the whole batch is retried with backoff
   (up to 5 attempts).
3. Compares the buildid against a persisted state file. On the first run it
   stores a baseline (no restart). When the buildid changes it updates the
   state and restarts the configured containers **one at a time** via the
   Docker socket.
4. After each `docker restart`, the service tails that container's logs and
   waits (up to `install_timeout` seconds) for the SteamCMD line
   `Success! App '<app_id>' fully installed.` before restarting the next
   container. On timeout it logs a WARNING and continues with the rest.
5. State is persisted to a named volume so restarts of the service itself do
   not cause false-positive container restarts.

## Files

| File                   | Purpose                                              |
|------------------------|------------------------------------------------------|
| `steam_server_watcher.py` | The monitoring application (Python 3.13, PyYAML)    |
| `Dockerfile`           | Multi-stage build: `steamcmd/steamcmd` → `python:3.13-slim-trixie` |
| `docker-compose.yml`   | Service definition (socket, config, state mounts)    |
| `config.yaml`          | Annotated sample configuration                       |

## Deployment

```bash
cd ./steam-server-watcher

# 1) Edit config.yaml — replace the example App IDs / branches / container
#    names with your own.
$EDITOR config.yaml

# 2) Build the image.
docker build -t steam-server-watcher:latest .

# 3) Start the stack.
docker compose up -d

# 4) Watch the logs.
docker compose logs -f steam-server-watcher
```

## Verification

```bash
# Confirm the container is healthy and running.
docker compose ps

# Run a single check cycle manually (does not loop) and watch it fetch buildids.
docker run --rm \
  -v "$PWD/config.yaml:/config/config.yaml:ro" \
  -v steam-server-watcher_steam-state:/state \
  steam-server-watcher:latest --once --verbose

# Inspect the persisted state (one file per app/branch).
docker run --rm -v steam-server-watcher_steam-state:/state \
  steam-server-watcher:latest sh -c 'ls -la /state; cat /state/*.buildid'
```

On first run each app logs `first run, baseline stored: ...` and writes a
`/state/app_<id>_<branch>.buildid` file. Subsequent runs log `No update` or,
when a new build lands, `update detected: ... buildid <old> -> <new>` followed
by `restarting container '<name>'` for each configured container.

## Configuration reference

See `config.yaml` for a fully annotated example. Key fields:

- `poll_interval` — global default polling interval in seconds.
- `steamcmd_path` — path to the steamcmd launcher inside the container.
- `state_dir` — directory for persisted buildid state (must match the compose mount).
- `install_timeout` — global default (seconds) to wait, after a restart, for a
  container's log to emit `Success! App '<app_id>' fully installed.` before
  moving to the next container. On timeout a WARNING is logged and the service
  continues. Can be overridden per-app.
- `apps[]` — list of apps to watch:
  - `app_id` (required) — Steam App ID (integer).
  - `branch` (optional, default `public`) — target branch.
  - `poll_interval` (optional) — per-app override of the global interval.
  - `install_timeout` (optional) — per-app override of the global install wait.
  - `containers` (optional) — list of Docker container names to restart on
    update, in the order they should be restarted. An empty list means log-only
    (no restart action).

## Notes

- The container runs as the non-root `steam` user (uid 1000).
- The Docker socket is mounted read-write so the service can issue
  `docker restart`. This grants the container control over the host daemon —
  only deploy on hosts you trust.
- SteamCMD is a 32-bit binary; the image carries the required i386 runtime
  libraries and exposes the 32-bit dynamic loader at `/lib/ld-linux.so.2`.