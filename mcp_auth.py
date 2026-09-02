"""Local, user-facing authentication for the Douyin MCP adapter.

The MCP server never returns cookie values. It only reports status and stores
the browser session in the original project's .env for compatibility.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, set_key, unset_key


class AuthError(RuntimeError):
    """Raised when local authentication is missing."""


class AuthManager:
    def __init__(self, project_root: str | None = None):
        default_root = Path(__file__).resolve().parent / "DouYin_Spider"
        if not default_root.exists():
            default_root = Path(__file__).resolve().parent
        self.project_root = Path(
            project_root or os.getenv("DOUYIN_PROJECT_ROOT") or default_root
        ).resolve()
        self.env_file = Path(
            os.getenv("DOUYIN_ENV_FILE") or self.project_root / ".env"
        ).resolve()
        self.sessions: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def _values(self) -> dict[str, str]:
        values = dotenv_values(str(self.env_file))
        return {str(k): str(v) for k, v in values.items() if v is not None}

    @staticmethod
    def _cookie_map(cookie_string: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for part in cookie_string.split(";"):
            if "=" in part:
                name, value = part.strip().split("=", 1)
                if name:
                    result[name] = value
        return result

    @staticmethod
    def _cookie_string(cookies: list[dict[str, Any]]) -> str:
        unique: dict[str, str] = {}
        for cookie in cookies:
            name = cookie.get("name")
            if name:
                unique[str(name)] = str(cookie.get("value", ""))
        return "; ".join(f"{name}={value}" for name, value in unique.items())

    @classmethod
    def _is_authenticated(cls, cookies: list[dict[str, Any]]) -> bool:
        names = {str(c.get("name")) for c in cookies}
        return bool(names.intersection({"sessionid", "sessionid_ss"}))

    def _original_auth(self, live: bool = False):
        import sys

        root = str(self.project_root)
        if root not in sys.path:
            sys.path.insert(0, root)
        from builder.auth import DouyinAuth

        values = self._values()
        key = "DY_LIVE_COOKIES" if live else "DY_COOKIES"
        cookie_string = values.get(key, "")
        if not cookie_string:
            raise AuthError(
                f"未找到 {key}。请先调用 douyin_login，让浏览器自动完成登录。"
            )
        auth = DouyinAuth()
        auth.perepare_auth(cookie_string, "", "")
        if not live:
            auth.ticket = values.get("DY_TICKET") or None
            auth.ts_sign = values.get("DY_TS_SIGN") or None
            auth.client_cert = values.get("DY_CLIENT_CERT") or None
            auth.private_key = values.get("DY_PRIVATE_KEY") or None
        return auth

    def get_auth(self):
        return self._original_auth(False)

    def get_live_auth(self):
        return self._original_auth(True)

    def status(self) -> dict[str, Any]:
        values = self._values()
        normal = self._cookie_map(values.get("DY_COOKIES", ""))
        live = self._cookie_map(values.get("DY_LIVE_COOKIES", ""))
        return {
            "env_file": str(self.env_file),
            "normal_login": bool({"sessionid", "sessionid_ss"}.intersection(normal)),
            "live_cookie_available": bool(live),
            "normal_cookie_names": sorted(normal),
            "live_cookie_names": sorted(live),
            "cookie_values_exposed": False,
        }

    def save_cookies(
        self, normal_cookies: list[dict[str, Any]], live_cookies: list[dict[str, Any]]
    ) -> None:
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        set_key(str(self.env_file), "DY_COOKIES", self._cookie_string(normal_cookies))
        set_key(str(self.env_file), "DY_LIVE_COOKIES", self._cookie_string(live_cookies))

    async def start_login(self, timeout_seconds: int = 180) -> dict[str, Any]:
        timeout_seconds = max(30, min(int(timeout_seconds), 600))
        session_id = secrets.token_urlsafe(12)
        self.sessions[session_id] = {
            "status": "starting",
            "started_at": int(time.time()),
            "timeout_seconds": timeout_seconds,
        }
        self._tasks[session_id] = asyncio.create_task(
            self._run_login(session_id, timeout_seconds)
        )
        return {
            "session_id": session_id,
            "status": "browser_opening",
            "message": "浏览器即将打开，请扫码或完成登录，然后调用 douyin_login_status。",
        }

    async def _run_login(self, session_id: str, timeout_seconds: int) -> None:
        try:
            from playwright.async_api import async_playwright

            self.sessions[session_id]["status"] = "waiting_for_user"
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=False)
                try:
                    context = await browser.new_context(locale="zh-CN")
                    page = await context.new_page()
                    await page.goto(
                        "https://www.douyin.com/",
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    deadline = time.monotonic() + timeout_seconds
                    normal_cookies: list[dict[str, Any]] = []
                    while time.monotonic() < deadline:
                        normal_cookies = await context.cookies(
                            ["https://www.douyin.com"]
                        )
                        if self._is_authenticated(normal_cookies):
                            break
                        await page.wait_for_timeout(1000)
                    if not self._is_authenticated(normal_cookies):
                        raise TimeoutError("等待抖音登录超时")

                    live_page = await context.new_page()
                    await live_page.goto(
                        "https://live.douyin.com/",
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    await live_page.wait_for_timeout(3000)
                    live_cookies = await context.cookies(["https://live.douyin.com"])
                    self.save_cookies(normal_cookies, live_cookies or normal_cookies)
                    self.sessions[session_id].update(
                        {"status": "success", "completed_at": int(time.time())}
                    )
                finally:
                    await browser.close()
        except Exception as exc:
            self.sessions[session_id].update(
                {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "completed_at": int(time.time()),
                }
            )

    def login_status(self, session_id: str) -> dict[str, Any]:
        if session_id not in self.sessions:
            return {"status": "unknown", "message": "登录会话不存在或服务已重启。"}
        result = dict(self.sessions[session_id])
        result["cookie_values_exposed"] = False
        return result

    def logout(self) -> dict[str, Any]:
        if self.env_file.exists():
            unset_key(str(self.env_file), "DY_COOKIES")
            unset_key(str(self.env_file), "DY_LIVE_COOKIES")
        return {"success": True, "message": "已清除本地抖音登录 Cookie。"}
