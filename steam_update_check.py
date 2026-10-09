#!/usr/bin/env python3
"""Steam game server update checker.

Polls SteamCMD for the buildid of configured game server apps and, when a
new build is detected, restarts the associated Docker containers.

Only third-party dependency: PyYAML.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    print("ERROR: PyYAML is required. Install with: pip install PyYAML", file=sys.stderr)
    sys.exit(2)

LOG = logging.getLogger("steam-update-check")

DEFAULT_CONFIG_PATH = "/config/config.yaml"
STEAMCMD_TIMEOUT = 180
DOCKER_TIMEOUT = 120
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = [5, 15, 30, 60]
DEFAULT_INSTALL_TIMEOUT = 300  # seconds to wait for the "fully installed" log line


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class AppConfig:
    """Configuration for a single Steam app to monitor."""

    app_id: int
    branch: str = "public"
    poll_interval: Optional[int] = None
    install_timeout: Optional[int] = None
    containers: List[str] = field(default_factory=list)


@dataclass
class Config:
    """Top-level configuration."""

    poll_interval: int = 300
    steamcmd_path: str = "steamcmd"
    state_dir: str = "/state"
    install_timeout: int = DEFAULT_INSTALL_TIMEOUT
    apps: List[AppConfig] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Config loading / validation
# ---------------------------------------------------------------------------

def _validate_app(raw: Any, index: int) -> AppConfig:
    """Validate and build a single AppConfig from a raw dict."""
    if not isinstance(raw, dict):
        raise ValueError(f"apps[{index}] must be a mapping")

    app_id = raw.get("app_id")
    if not isinstance(app_id, int) or isinstance(app_id, bool) or app_id <= 0:
        raise ValueError(f"apps[{index}].app_id must be a positive integer, got {app_id!r}")

    branch = raw.get("branch", "public")
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError(f"apps[{index}].branch must be a non-empty string, got {branch!r}")

    poll_interval = raw.get("poll_interval")
    if poll_interval is not None:
        if not isinstance(poll_interval, int) or isinstance(poll_interval, bool) or poll_interval <= 0:
            raise ValueError(
                f"apps[{index}].poll_interval must be a positive integer, got {poll_interval!r}"
            )

    install_timeout = raw.get("install_timeout")
    if install_timeout is not None:
        if not isinstance(install_timeout, int) or isinstance(install_timeout, bool) or install_timeout <= 0:
            raise ValueError(
                f"apps[{index}].install_timeout must be a positive integer, got {install_timeout!r}"
            )

    containers = raw.get("containers", [])
    if containers is None:
        containers = []
    if not isinstance(containers, list):
        raise ValueError(f"apps[{index}].containers must be a list, got {type(containers).__name__}")
    for i, c in enumerate(containers):
        if not isinstance(c, str) or not c.strip():
            raise ValueError(f"apps[{index}].containers[{i}] must be a non-empty string, got {c!r}")

    return AppConfig(
        app_id=app_id,
        branch=branch.strip(),
        poll_interval=poll_interval,
        install_timeout=install_timeout,
        containers=[c.strip() for c in containers],
    )


def load_config(path: str) -> Config:
    """Load and validate the YAML configuration file."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"config file not found: {path}")

    with open(p, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError("top-level config must be a mapping")

    poll_interval = raw.get("poll_interval", 300)
    if not isinstance(poll_interval, int) or isinstance(poll_interval, bool) or poll_interval <= 0:
        raise ValueError(f"poll_interval must be a positive integer, got {poll_interval!r}")

    steamcmd_path = raw.get("steamcmd_path", "steamcmd")
    if not isinstance(steamcmd_path, str) or not steamcmd_path.strip():
        raise ValueError(f"steamcmd_path must be a non-empty string, got {steamcmd_path!r}")

    state_dir = raw.get("state_dir", "/state")
    if not isinstance(state_dir, str) or not state_dir.strip():
        raise ValueError(f"state_dir must be a non-empty string, got {state_dir!r}")

    install_timeout = raw.get("install_timeout", DEFAULT_INSTALL_TIMEOUT)
    if not isinstance(install_timeout, int) or isinstance(install_timeout, bool) or install_timeout <= 0:
        raise ValueError(f"install_timeout must be a positive integer, got {install_timeout!r}")

    raw_apps = raw.get("apps", [])
    if not isinstance(raw_apps, list) or not raw_apps:
        raise ValueError("'apps' must be a non-empty list")

    apps = [_validate_app(a, i) for i, a in enumerate(raw_apps)]

    return Config(
        poll_interval=poll_interval,
        steamcmd_path=steamcmd_path.strip(),
        state_dir=state_dir.strip(),
        install_timeout=install_timeout,
        apps=apps,
    )


# ---------------------------------------------------------------------------
# SteamCMD buildid fetch
# ---------------------------------------------------------------------------

def _extract_buildid(block: str, branch: str) -> Optional[int]:
    """Extract the buildid for *branch* from a single app's ``app_info_print`` block.

    SteamCMD's ``app_info_print`` emits a JSON-like (tab-indented, unquoted
    braces, tab-separated key/value, no colons) document. The per-branch build
    identifier lives at::

        "depots" { "<depot>" {
            "manifests" { "<branch>" { "gid" "..." ... } ... }
            "branches"  { "<branch>" { "buildid" "<n>" ... } ... }
        } } }

    Note the branch name appears as a key in BOTH the ``manifests`` and
    ``branches`` blocks, so we must anchor on the ``branches`` block and then
    find the branch key *within it*, then read the following ``buildid``.
    """
    # 1) Locate the "branches" block (the one that contains buildid values).
    branches_block = re.search(r'["\']\s*branches\s*["\']', block)
    if not branches_block:
        return None
    search_from = branches_block.end()

    # 2) Within the branches block, find the branch key.
    branch_key_re = re.compile(r'["\']\s*' + re.escape(branch) + r'\s*["\']')
    m = branch_key_re.search(block, search_from)
    if not m:
        return None

    # 3) Read the next "buildid" <number> after the branch key.
    #    Format is:  "buildid"\t\t"25793607"   (tab-separated, quoted value,
    #    no colon). Allow optional colon/whitespace/quotes for robustness.
    tail = block[m.end():]
    buildid_re = re.compile(r'["\']\s*buildid\s*["\']\s*[:=]?\s*["\']?\s*(\d+)')
    bm = buildid_re.search(tail)
    if not bm:
        return None
    return int(bm.group(1))


def _split_app_blocks(output: str) -> dict:
    """Split combined SteamCMD output into ``{app_id: block_text}``.

    When multiple ``+app_info_print`` commands are issued in one steamcmd
    session, the output contains one block per app, each preceded by a header
    line of the form::

        AppID : 443030, change number : 39637111/39637111, last change : ...

    We use those headers as anchors so each app's buildid is parsed from its
    own block (a single-app call also works, since it has exactly one header).
    """
    blocks: dict = {}
    header_re = re.compile(r'AppID\s*:\s*(\d+)')
    matches = list(header_re.finditer(output))
    if not matches:
        # No headers (e.g. a failure) — fall back to one anonymous block.
        return {"_all": output}
    for i, m in enumerate(matches):
        app_id = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(output)
        blocks[app_id] = output[start:end]
    return blocks


def fetch_buildids(steamcmd_path: str, app_specs: list) -> dict:
    """Fetch buildids for many apps in a SINGLE steamcmd invocation.

    *app_specs* is a list of ``(app_id, branch)`` tuples. Returns a dict
    mapping ``app_id -> buildid`` (failed apps are simply absent).

    Batching all apps into one steamcmd session is more efficient than one
    process per app: a single Steam connection, one API load, and one
    "Waiting for user info" handshake instead of N.
    """
    if not app_specs:
        return {}

    cmd = [steamcmd_path, "+login", "anonymous"]
    for app_id, _branch in app_specs:
        cmd += ["+app_info_print", str(app_id)]
    cmd.append("+quit")

    results: dict = {}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        LOG.debug("steamcmd attempt %d/%d: %s", attempt, MAX_ATTEMPTS, " ".join(cmd))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=STEAMCMD_TIMEOUT)
        except subprocess.TimeoutExpired:
            LOG.error("steamcmd timed out after %ds (attempt %d/%d)", STEAMCMD_TIMEOUT, attempt, MAX_ATTEMPTS)
        except FileNotFoundError:
            LOG.error("steamcmd binary not found at %s", steamcmd_path)
            return results
        except Exception as exc:  # noqa: BLE001
            LOG.error("steamcmd failed (attempt %d/%d): %s", attempt, MAX_ATTEMPTS, exc)
        else:
            combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
            blocks = _split_app_blocks(combined)
            for app_id, branch in app_specs:
                if app_id in results:
                    continue
                block = blocks.get(app_id, blocks.get("_all", ""))
                buildid = _extract_buildid(block, branch)
                if buildid is not None:
                    results[app_id] = buildid
                    LOG.info("fetched buildid %d for app %d branch %r (attempt %d)", buildid, app_id, branch, attempt)

            if len(results) == len(app_specs):
                return results

            missing = [a for a, _b in app_specs if a not in results]
            if proc.returncode == 254:
                LOG.warning(
                    "steamcmd connection failed (attempt %d/%d, rc=254 = Steam unreachable); still missing: %s; retrying",
                    attempt, MAX_ATTEMPTS, missing,
                )
            else:
                LOG.warning(
                    "could not resolve buildid for %s (attempt %d/%d, rc=%d); retrying",
                    missing, attempt, MAX_ATTEMPTS, proc.returncode,
                )

        if attempt < MAX_ATTEMPTS:
            delay = BACKOFF_SECONDS[attempt - 1] if attempt - 1 < len(BACKOFF_SECONDS) else BACKOFF_SECONDS[-1]
            LOG.debug("backing off %ds before next attempt", delay)
            time.sleep(delay)

    missing = [a for a, _b in app_specs if a not in results]
    if missing:
        LOG.error("all %d attempts to fetch buildid(s) failed for: %s", MAX_ATTEMPTS, missing)
    return results


def fetch_buildid(steamcmd_path: str, app_id: int, branch: str) -> Optional[int]:
    """Fetch the current buildid for a single *app_id*/*branch* via SteamCMD.

    Convenience wrapper around :func:`fetch_buildids` for one app.
    """
    return fetch_buildids(steamcmd_path, [(app_id, branch)]).get(app_id)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _state_file_path(state_dir: str, app_id: int, branch: str) -> Path:
    safe_branch = re.sub(r"[^A-Za-z0-9._-]", "_", branch)
    return Path(state_dir) / f"app_{app_id}_{safe_branch}.buildid"


def _write_atomic(path: Path, data: str) -> None:
    """Write *data* to *path* atomically (temp file + os.replace)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def check_and_update_state(state_dir: str, app_id: int, branch: str, buildid: int) -> str:
    """Compare *buildid* against stored state.

    Returns:
        "first_run"  – no prior state; baseline stored, no restart.
        "updated"    – buildid changed; state updated, restart should happen.
        "unchanged"  – buildid identical; nothing to do.
    """
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    sf = _state_file_path(state_dir, app_id, branch)

    if not sf.is_file():
        _write_atomic(sf, str(buildid))
        LOG.info("first run, baseline stored: app %d branch %r buildid %d", app_id, branch, buildid)
        return "first_run"

    try:
        old = int(sf.read_text(encoding="utf-8").strip())
    except (ValueError, OSError) as exc:
        LOG.warning("could not read state file %s (%s); treating as first run", sf, exc)
        _write_atomic(sf, str(buildid))
        LOG.info("first run (recovered), baseline stored: app %d branch %r buildid %d", app_id, branch, buildid)
        return "first_run"

    if old == buildid:
        return "unchanged"

    LOG.info("update detected: app %d branch %r buildid %d -> %d", app_id, branch, old, buildid)
    _write_atomic(sf, str(buildid))
    return "updated"


# ---------------------------------------------------------------------------
# Container restart
# ---------------------------------------------------------------------------

def _container_is_running(name: str) -> bool:
    """Return True if container *name* exists and is currently running."""
    try:
        proc = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):  # noqa: BLE001
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _wait_for_install_line(container: str, app_id: int, timeout: int) -> bool:
    """Tail *container*'s logs and wait for the SteamCMD "fully installed" line.

    Waits up to *timeout* seconds for a line matching
    ``Success! App '<app_id>' fully installed.`` (the exact App ID is
    substituted). Returns True as soon as the line is seen, or False on
    timeout / if the log stream ends without it. Never raises.
    """
    needle = f"Success! App '{app_id}' fully installed."
    LOG.info("waiting up to %ds for %r to log: %r", timeout, container, needle)

    cmd = ["docker", "logs", "-f", "--tail", "0", container]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        LOG.error("docker binary not found; cannot tail logs for %r", container)
        return False
    except Exception as exc:  # noqa: BLE001
        LOG.error("failed to start log tail for %r: %s", container, exc)
        return False

    deadline = time.monotonic() + timeout
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if needle in line:
                LOG.info("container %r reported: %r", container, needle)
                return True
            if time.monotonic() > deadline:
                break
    finally:
        # Stop the follow stream.
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    LOG.warning(
        "timed out after %ds waiting for %r to log %r; continuing",
        timeout, container, needle,
    )
    return False


def restart_containers(
    containers: List[str],
    app_id: int,
    install_timeout: int,
) -> None:
    """Restart Docker containers strictly one at a time, waiting for readiness.

    Containers are restarted sequentially in the order given. After each
    ``docker restart`` succeeds, the service tails that container's logs and
    waits (up to *install_timeout* seconds) for the SteamCMD line
    ``Success! App '<app_id>' fully installed.`` before touching the next
    container. A timeout or failure on one container is logged and recorded,
    but the remaining containers are still attempted (no single failure
    aborts the whole sequence). This function never raises.
    """
    if not containers:
        LOG.info("no containers configured for restart")
        return

    total = len(containers)
    failures: List[str] = []
    for idx, name in enumerate(containers, start=1):
        LOG.info("restarting container %d/%d: %r", idx, total, name)
        try:
            proc = subprocess.run(
                ["docker", "restart", name],
                capture_output=True,
                text=True,
                timeout=DOCKER_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            LOG.error("docker restart %r timed out after %ds", name, DOCKER_TIMEOUT)
            failures.append(name)
            continue
        except FileNotFoundError:
            LOG.error("docker binary not found; cannot restart %r", name)
            failures.append(name)
            continue
        except Exception as exc:  # noqa: BLE001
            LOG.error("docker restart %r failed: %s", name, exc)
            failures.append(name)
            continue

        if proc.returncode != 0:
            LOG.error("container %r restart failed (rc=%d): %s", name, proc.returncode, (proc.stderr or "").strip())
            failures.append(name)
            continue

        # Confirm the container is up, then wait for the install-complete line
        # before proceeding to the next container.
        if not _container_is_running(name):
            LOG.warning("container %r restarted (rc=0) but is not reported as running yet", name)

        if _wait_for_install_line(name, app_id, install_timeout):
            LOG.info("container %r ready; proceeding to next", name)
        else:
            LOG.warning("container %r did not confirm install within timeout; proceeding to next", name)

    if failures:
        LOG.error("restart summary: %d/%d failed: %s", len(failures), total, ", ".join(failures))
    else:
        LOG.info("restart summary: all %d container(s) restarted sequentially", total)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_cycle(cfg: Config) -> None:
    """Execute one polling cycle across all configured apps.

    All apps are fetched in a SINGLE steamcmd invocation (one Steam
    connection) for efficiency, then each app's buildid is compared against
    its persisted state and containers restarted as needed.
    """
    app_specs = [(app.app_id, app.branch) for app in cfg.apps]
    buildids = fetch_buildids(cfg.steamcmd_path, app_specs)

    for app in cfg.apps:
        try:
            buildid = buildids.get(app.app_id)
            if buildid is None:
                LOG.error("skipping app %d (branch %r): buildid fetch failed", app.app_id, app.branch)
                continue

            result = check_and_update_state(cfg.state_dir, app.app_id, app.branch, buildid)
            if result == "updated":
                timeout = app.install_timeout if app.install_timeout is not None else cfg.install_timeout
                restart_containers(app.containers, app.app_id, timeout)
        except Exception:  # noqa: BLE001
            LOG.exception("unexpected error processing app %d (branch %r)", app.app_id, app.branch)


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Steam game server update checker")
    parser.add_argument(
        "--config",
        default=os.environ.get("SUC_CONFIG", DEFAULT_CONFIG_PATH),
        help="Path to YAML config (default: $SUC_CONFIG or %(default)s)",
    )
    parser.add_argument("--once", action="store_true", help="Run a single cycle then exit")
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )

    try:
        cfg = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        LOG.error("configuration error: %s", exc)
        return 2

    LOG.info(
        "loaded config: %d app(s), poll_interval=%ds, state_dir=%s",
        len(cfg.apps), cfg.poll_interval, cfg.state_dir,
    )

    try:
        if args.once:
            run_cycle(cfg)
        else:
            while True:
                run_cycle(cfg)
                intervals = [a.poll_interval if a.poll_interval else cfg.poll_interval for a in cfg.apps]
                sleep_for = max(intervals) if intervals else cfg.poll_interval
                LOG.debug("sleeping %ds until next cycle", sleep_for)
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        LOG.info("shutting down")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
