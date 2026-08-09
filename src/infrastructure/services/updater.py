"""Self-update check: is a newer revision available on GitHub than the one we're running?

The product ships via git-pull + `docker compose build` (scripts/update.sh). The bot runs
inside a container and can't touch the host git/daemon, so it can't compute "latest" locally.
Instead it compares the git short-SHA baked into the image at build time (``APP__BUILD_SHA``,
set by install.sh/update.sh) against the latest commit of the tracked branch on GitHub.

Everything is fail-soft: a network blip or a missing build SHA just reports "no update".
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from src.core.logging import get_logger

log = get_logger(__name__)

_GITHUB_API = "https://api.github.com"

# The «Обновить» / restart buttons write this marker; the updater sidecar watches it, acts on
# the first line, then removes it. It's a volume mounted in both the app and the updater.
UPDATE_REQUEST_FILE = "/app/update-signals/request"
_HEARTBEAT_FILE = "/app/update-signals/.alive"
_HEARTBEAT_MAX_AGE = 60  # the sidecar touches it every ~10s

_RESTARTABLE = ("bot", "web", "worker", "scheduler", "all")


def _updater_alive() -> bool:
    """True only when the updater SIDECAR is actually running. The signals volume is mounted into
    every app container regardless, so directory existence alone would falsely report 'connected'
    when the updater profile is off or the sidecar crash-looped — the marker would then rot unread.
    The sidecar refreshes a heartbeat file each loop; we trust it only while it's fresh."""
    import time
    from pathlib import Path

    try:
        st = Path(_HEARTBEAT_FILE).stat()
    except OSError:
        return False
    return (time.time() - st.st_mtime) < _HEARTBEAT_MAX_AGE


def signals_writable() -> bool:
    """The sidecar is alive but the shared volume may be owned by another uid — we run as the
    unprivileged `app` user. Distinguishes «updater not wired» from «wired but I can't write»,
    which used to surface as the same misleading «модуль обновлений не подключён»."""
    import os
    from pathlib import Path

    return os.access(Path(UPDATE_REQUEST_FILE).parent, os.W_OK)


def _write_marker(content: str) -> bool:
    """Drop a request marker for the updater sidecar. Returns False when the sidecar isn't running
    (so the caller honestly tells the operator to act by hand) or the write fails."""
    from pathlib import Path

    path = Path(UPDATE_REQUEST_FILE)
    if not path.parent.is_dir() or not _updater_alive():
        return False
    try:
        path.write_text(content + "\n")
        return True
    except OSError as exc:
        log.warning("updater request write failed", error=str(exc), request=content)
        return False


def request_update() -> bool:
    """Ask the updater sidecar to pull + rebuild + restart (scripts/update.sh)."""
    return _write_marker("update")


def request_restart(service: str) -> bool:
    """Ask the updater sidecar to `docker compose restart <service>` (bot/web/worker/scheduler,
    or 'all'). Returns False if the volume isn't mounted or the service name isn't allowed."""
    if service not in _RESTARTABLE:
        return False
    return _write_marker(f"restart {service}")


def _is_same_commit(a: str, b: str) -> bool:
    """True when two git SHAs denote the same commit, tolerating different abbreviation widths.

    The build SHA baked into the image is a 7-char ``git rev-parse --short`` (install.sh/update.sh),
    while GitHub returns the full 40-char sha. A plain ``!=`` between a 7-char and a 12-char string
    is therefore ALWAYS true — even on the identical commit — so the checker used to report an
    update forever (the "you're up to date" branch was unreachable, and auto-update re-armed every
    6 h). Compare on the shorter length's prefix — exactly how git resolves an abbreviated SHA.
    """
    a, b = a.strip().lower(), b.strip().lower()
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return a[:n] == b[:n]


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    current: str  # short SHA we're running (build_sha), or "" if unknown
    latest: str  # latest short SHA on the tracked branch, or "" if unavailable
    available: bool  # a real, different, newer-or-unknown revision exists
    message: str  # latest commit subject (first line), for the notification
    url: str  # compare/commit URL for the operator to inspect


async def check_for_update(
    repo: str, branch: str, build_sha: str, *, timeout: float = 8.0
) -> UpdateInfo:
    """Return whether GitHub ``repo``@``branch`` is ahead of our ``build_sha``. Never raises."""
    current = (build_sha or "").strip()[:12]
    empty = UpdateInfo(current=current, latest="", available=False, message="", url="")
    if not repo or "/" not in repo:
        return empty
    url = f"{_GITHUB_API}/repos/{repo}/commits/{branch or 'main'}"
    try:
        # follow_redirects: a RENAMED repo answers 301 with the new location. Without this every
        # already-installed bot (whose saved UPDATE_REPO still holds the old name) reported
        # "GitHub check failed" forever.
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url, headers={"Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            log.info("update check non-200", status=r.status_code)
            return empty
        data = r.json()
    except Exception as exc:
        log.info("update check failed", error=str(exc))
        return empty

    latest_full = str(data.get("sha") or "")
    latest = latest_full[:12]
    subject = str(((data.get("commit") or {}).get("message") or "").split("\n", 1)[0])[:200]
    if not latest:
        return empty
    # Unknown local SHA (image built without the build-arg) → surface the update so the operator
    # can still act, but we can't claim it's strictly newer. Compare against the full sha via a
    # width-tolerant prefix match so a 7-char build SHA is correctly seen as "same" (not "behind").
    available = (not current) or not _is_same_commit(current, latest_full)
    compare = (
        f"https://github.com/{repo}/compare/{current}...{latest}"
        if current
        else f"https://github.com/{repo}/commit/{latest_full}"
    )
    return UpdateInfo(
        current=current, latest=latest, available=available, message=subject, url=compare
    )
