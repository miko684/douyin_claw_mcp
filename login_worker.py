"""Open a visible Douyin browser and save local cookies after user login."""

from __future__ import annotations

import asyncio
import os
import sys

from mcp_auth import AuthManager


async def run() -> int:
    auth = AuthManager(os.getenv("DOUYIN_PROJECT_ROOT"))
    current = auth.status()
    if current["normal_login"] and current["live_cookie_available"]:
        return 0

    session = await auth.start_login(timeout_seconds=600)
    task = auth._tasks[session["session_id"]]
    await task
    result = auth.login_status(session["session_id"])
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run()))
    except KeyboardInterrupt:
        raise SystemExit(130)
