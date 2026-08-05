"""Liveness signalling and board-level failure detection.

The dangerous failure mode for this bot is silence: if it stops running, the
channel simply goes quiet, which looks exactly like a day with no new
notices. The user would only find out by missing a deadline — the very thing
the bot exists to prevent.

Two independent signals cover that:

  ping()          an outside watchdog (healthchecks.io) alerts when the
                  pings stop, which works even when the runner is dead
  failing_boards() a board whose listing keeps failing is reported into the
                  channel, which covers "running fine, but blind"
"""

import os
import urllib.request

from . import db

PING_TIMEOUT = 10
FAILURE_STREAK = 3  # consecutive failed runs before the channel is told


CHECK_URL_ENV = "CUK_HEALTHCHECK_URL"
DIGEST_URL_ENV = "CUK_HEALTHCHECK_DIGEST_URL"


def ping(state: str = "success", env: str = CHECK_URL_ENV, log=print) -> bool:
    """Signal the watchdog. `state` is 'start', 'success' or 'fail'.

    Never raises: a monitoring call must not be able to break the run it is
    monitoring. Returns False when no URL is configured, so a deployment
    without a watchdog degrades quietly rather than erroring every run.

    `--check` and `--digest` use separate monitors: the digest runs daily, so
    its 100-entry free-tier log covers months, while a shared monitor would
    be flooded by the 30-minute check.
    """
    base = os.environ.get(env)
    if not base:
        return False

    url = base.rstrip("/")
    if state == "start":
        url += "/start"
    elif state == "fail":
        url += "/fail"

    try:
        urllib.request.urlopen(url, timeout=PING_TIMEOUT).read()
        return True
    except Exception as exc:
        log(f"  [!] 헬스체크 ping 실패({state}): {str(exc)[:80]}")
        return False


def failing_boards(con, streak: int = FAILURE_STREAK) -> list:
    """Boards whose last `streak` runs all failed.

    Uses the tail of crawl_log per board rather than a global count, so one
    broken board is reported while the other seven keep working.
    """
    out = []
    board_ids = [r[0] for r in con.execute(
        "SELECT DISTINCT board_id FROM crawl_log")]

    for board_id in board_ids:
        # Ordered by rowid, not ran_at: the timestamp has second granularity,
        # so runs landing in the same second sort arbitrarily and a recovery
        # could be read as still-failing. Insertion order is monotonic.
        recent = [r["ok"] for r in con.execute(
            "SELECT ok FROM crawl_log WHERE board_id=? "
            "ORDER BY rowid DESC LIMIT ?", (board_id, streak))]
        if len(recent) == streak and not any(recent):
            out.append(board_id)
    return out


def already_warned(con, board_id: str) -> bool:
    return con.execute("SELECT 1 FROM meta WHERE key=?",
                       (f"warned:{board_id}",)).fetchone() is not None


def mark_warned(con, board_id: str) -> None:
    """Remember that this board was reported, so it is not repeated hourly."""
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                (f"warned:{board_id}", db.now()))
    con.commit()


def clear_warning(con, board_id: str) -> None:
    """Called when a board parses again — the next breakage gets a fresh alert."""
    con.execute("DELETE FROM meta WHERE key=?", (f"warned:{board_id}",))
    con.commit()


def new_failures(con, streak: int = FAILURE_STREAK) -> list:
    """Boards that just crossed the failure threshold and were not yet told."""
    failing = failing_boards(con, streak)
    for board_id in [r[0] for r in con.execute(
            "SELECT DISTINCT board_id FROM crawl_log")]:
        if board_id not in failing:
            clear_warning(con, board_id)
    return [b for b in failing if not already_warned(con, b)]
