"""Local Douyin MCP server.

Run with the MCP stdio transport. It wraps the existing Douyin_Spider code
without modifying that project. Cookie values are never returned as output.
"""

from __future__ import annotations

import asyncio
import atexit
import copy
import contextlib
import io
import inspect
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import webbrowser
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image
from mcp.types import CallToolResult, TextContent

from live_monitor import LiveMonitor
from mcp_auth import AuthManager
from private_monitor import PrivateMonitor
from mcp_stats import StatsStore


_TRACKED_MCP_TOOL: ContextVar[str] = ContextVar("douyin_tracked_mcp_tool", default="")


_BUNDLED_PROJECT_ROOT = Path(__file__).resolve().parent / "DouYin_Spider"
_CANONICAL_STATS_PATH = Path(__file__).resolve().parent / "work" / "mcp_stats.json"
PROJECT_ROOT = os.getenv("DOUYIN_PROJECT_ROOT") or str(_BUNDLED_PROJECT_ROOT)
AUTH = AuthManager(PROJECT_ROOT)
LIVE_MONITORS: dict[str, LiveMonitor] = {}
PRIVATE_MONITORS: dict[str, PrivateMonitor] = {}
STATS = StatsStore()
_DASHBOARD_STATS = (
    STATS
    if STATS.path.resolve() == _CANONICAL_STATS_PATH.resolve()
    else StatsStore(str(_CANONICAL_STATS_PATH))
)
_STATS_TARGETS = (STATS,) if _DASHBOARD_STATS is STATS else (STATS, _DASHBOARD_STATS)
_DASHBOARD_EXTERNAL_OPENED = False
_CACHE_TTL_SECONDS = 300
_CACHE_LOCK = asyncio.Lock()
_API_CALL_LOCK = asyncio.Lock()
_RESULT_CACHE: dict[tuple[Any, ...], tuple[float, Any]] = {}
_INFLIGHT: dict[tuple[Any, ...], asyncio.Task[Any]] = {}
_SINGLETON_LOCK_HANDLE: Any = None
_SINGLETON_LOCK_OWNED = False
_COVER_REQUIRED_TOOLS = {
    "get_work_info",
    "get_user_works",
    "search_videos",
    "get_favorite_list",
    "get_feed",
}


def _acquire_singleton_lock() -> None:
    """Elect one worker as the shared-status owner without rejecting clients.

    MCP stdio uses one worker process per client/reconnect. A hard singleton
    exit makes a healthy server look offline to the second client, so a lock
    collision is now only a status-ownership collision.
    """
    global _SINGLETON_LOCK_HANDLE, _SINGLETON_LOCK_OWNED
    lock_path = _CANONICAL_STATS_PATH.with_name("mcp_server.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    if lock_path.stat().st_size == 0:
        handle.write("0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, PermissionError):
        handle.close()
        sys.stderr.write("Douyin MCP 已有实例运行，本进程继续提供 stdio 服务。\n")
        return
    _SINGLETON_LOCK_HANDLE = handle
    _SINGLETON_LOCK_OWNED = True
    atexit.register(_release_singleton_lock)


def _stats_log(message: str, level: str = "info", tool: str = "") -> None:
    """Write logs to the caller's store and the dashboard's canonical store."""
    for stats in _STATS_TARGETS:
        stats.log(message, level, tool)


def _stats_record(
    tool_name: str,
    result: Any = None,
    operation_id: str | None = None,
) -> None:
    """Mirror tool results so the dashboard cannot drift from an MCP client."""
    for stats in _STATS_TARGETS:
        stats.record(tool_name, result, operation_id)


def _stats_begin(
    operation_id: str,
    tool_name: str,
    parameters: dict[str, Any],
) -> None:
    for stats in _STATS_TARGETS:
        stats.begin_operation(operation_id, tool_name, parameters)


def _stats_fail(operation_id: str, tool_name: str, exc: Exception) -> None:
    error = f"{type(exc).__name__}: {str(exc)}"
    for stats in _STATS_TARGETS:
        stats.fail_operation(operation_id, tool_name, error)


def _stats_fail_safely(operation_id: str, tool_name: str, exc: Exception) -> None:
    """Best-effort failure marker that never hides the original exception."""
    try:
        _stats_fail(operation_id, tool_name, exc)
    except Exception as stats_exc:
        sys.stderr.write(
            f"Douyin MCP 记录失败状态时出错: {type(stats_exc).__name__}: {stats_exc}\n"
        )


def _safe_operation_parameters(
    fn: Callable[..., Any],
    fn_args: tuple[Any, ...],
    fn_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Keep useful local inputs without allowing huge/non-JSON values."""
    try:
        values = inspect.signature(fn).bind_partial(*fn_args, **fn_kwargs).arguments
    except (TypeError, ValueError):
        values = fn_kwargs

    def clean(value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:500]
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value[:20]]
        if isinstance(value, dict):
            return {str(key)[:80]: clean(item) for key, item in list(value.items())[:20]}
        return str(value)[:500]

    return {str(name): clean(value) for name, value in values.items()}


def _stats_set_status(status: str) -> None:
    for stats in _STATS_TARGETS:
        stats.set_status(status)


def _release_singleton_lock() -> None:
    global _SINGLETON_LOCK_HANDLE, _SINGLETON_LOCK_OWNED
    handle = _SINGLETON_LOCK_HANDLE
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()
        _SINGLETON_LOCK_HANDLE = None
        _SINGLETON_LOCK_OWNED = False


async def _cached_result(
    key: tuple[Any, ...],
    loader: Callable[[], Any],
    refresh: bool = False,
) -> Any:
    """Avoid duplicate network crawls for the same request in one MCP run."""
    now = time.monotonic()
    if not refresh:
        cached = _RESULT_CACHE.get(key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return copy.deepcopy(cached[1])

        async with _CACHE_LOCK:
            cached = _RESULT_CACHE.get(key)
            if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
                return copy.deepcopy(cached[1])
            task = _INFLIGHT.get(key)
            if task is None:
                task = asyncio.create_task(loader())
                _INFLIGHT[key] = task
        try:
            result = await task
        except Exception:
            async with _CACHE_LOCK:
                if _INFLIGHT.get(key) is task:
                    _INFLIGHT.pop(key, None)
            raise
        async with _CACHE_LOCK:
            _RESULT_CACHE[key] = (time.monotonic(), copy.deepcopy(result))
            if _INFLIGHT.get(key) is task:
                _INFLIGHT.pop(key, None)
        return copy.deepcopy(result)

    return await loader()


def _first_cover_url(value: Any) -> str:
    if isinstance(value, str):
        return value if value.startswith(("http://", "https://", "data:image/", "/api/artifacts/")) else ""
    if isinstance(value, list):
        for item in value:
            url = _first_cover_url(item)
            if url:
                return url
        return ""
    if not isinstance(value, dict):
        return ""
    for key in (
        "video_cover", "cover_url", "origin_cover", "cover", "dynamic_cover",
        "url_list", "images", "video", "aweme_info",
    ):
        url = _first_cover_url(value.get(key))
        if url:
            return url
    return ""


def _cover_work_id(item: dict[str, Any], index: int) -> str:
    nested = item.get("aweme_info") if isinstance(item.get("aweme_info"), dict) else {}
    value = item.get("work_id") or item.get("aweme_id") or nested.get("aweme_id") or f"item-{index}"
    return re.sub(r"[^0-9A-Za-z_-]", "_", str(value))[:100]


def _cover_items(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []
    for key in ("aweme_list", "collects", "data", "items"):
        value = result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return [result]


def _download_cover(url: str, work_id: str) -> str:
    if url.startswith(("data:image/", "/api/artifacts/")):
        return url
    import requests

    cover_root = Path(__file__).resolve().parent / "work" / "covers"
    cover_root.mkdir(parents=True, exist_ok=True)
    with requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151.0.0.0 Safari/537.36",
            "Referer": "https://www.douyin.com/",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
        cookies=AUTH.get_auth().cookie,
        stream=True,
        timeout=30,
    ) as response:
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        extension = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "image/avif": ".avif",
        }.get(content_type, ".jpg")
        target = cover_root / f"{work_id}{extension}"
        temporary = cover_root / f".{work_id}.{uuid.uuid4().hex}.tmp"
        size = 0
        try:
            with temporary.open("wb") as cover_file:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > 20 * 1024 * 1024:
                        raise ValueError("封面文件超过 20MB")
                    cover_file.write(chunk)
            if size == 0:
                raise ValueError("封面响应为空")
            if not content_type.startswith("image/"):
                signature = temporary.read_bytes()[:12]
                if not (
                    signature.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8"))
                    or signature[4:12] == b"ftypavif"
                    or signature[8:12] == b"WEBP"
                ):
                    raise ValueError("封面地址返回的不是图片")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return f"/api/artifacts/covers/{target.name}"


async def _ensure_video_covers(result: Any) -> Any:
    """Persist covers when possible without discarding collected results."""
    items = _cover_items(result)
    if not items:
        return result

    failures: list[str] = []

    async def persist(item: dict[str, Any], index: int) -> None:
        work_id = _cover_work_id(item, index)
        cover_url = _first_cover_url(item)
        if not cover_url:
            failures.append(f"{work_id}: missing_cover")
            return
        try:
            local_url = await asyncio.to_thread(_download_cover, cover_url, work_id)
        except Exception as exc:
            failures.append(f"{work_id}: {type(exc).__name__}")
            return
        item["video_cover"] = local_url

    await asyncio.gather(*(persist(item, index) for index, item in enumerate(items)))
    if failures:
        preview = ", ".join(failures[:5])
        _stats_log(
            f"封面本地化失败 {len(failures)}/{len(items)}，已保留抓取结果: {preview}",
            "warning",
            "covers",
        )
    return result


def _dashboard_url() -> str:
    port = os.getenv("DOUYIN_DASHBOARD_PORT", "8765")
    return os.getenv("DOUYIN_DASHBOARD_URL") or f"http://127.0.0.1:{port}/"


def _dashboard_ready(url: str) -> bool:
    """Return whether the local dashboard port is accepting connections."""
    try:
        host = url.split("//", 1)[1].split("/", 1)[0]
        hostname, port_text = host.rsplit(":", 1)
        with socket.create_connection((hostname, int(port_text)), timeout=0.25):
            return True
    except (OSError, ValueError, IndexError):
        return False


def _ensure_dashboard() -> None:
    """Start the local dashboard if needed, then open it in the default browser."""
    global _DASHBOARD_EXTERNAL_OPENED
    # A stdio MCP cannot control Codex's in-app browser. Always keep the
    # dashboard server running locally, but only open an external browser
    # when the user explicitly requests it.
    open_external = os.getenv("DOUYIN_OPEN_DASHBOARD", "0").lower() not in {"0", "false", "no"}
    url = _dashboard_url()
    if not _dashboard_ready(url):
        dashboard_script = Path(__file__).resolve().with_name("dashboard_server.py")
        env = dict(os.environ)
        env.setdefault("DOUYIN_PROJECT_ROOT", str(AUTH.project_root))
        env["DOUYIN_STATS_FILE"] = str(_CANONICAL_STATS_PATH)
        try:
            launcher = dashboard_script.with_name("run_local.py")
            subprocess.Popen(
                [sys.executable, str(launcher), dashboard_script.name],
                cwd=str(dashboard_script.parent),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            return
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not _dashboard_ready(url):
            time.sleep(0.1)
    if open_external and not _DASHBOARD_EXTERNAL_OPENED:
        _DASHBOARD_EXTERNAL_OPENED = True
        try:
            webbrowser.open(url, new=0, autoraise=True)
        except webbrowser.Error:
            pass


class TrackingMCPServer(MCPServer):
    """Register normal MCP tools while recording non-sensitive local metrics."""

    def tool(self, *args: Any, **kwargs: Any):
        parent_decorator = super().tool(*args, **kwargs)

        def decorator(fn):
            if asyncio.iscoroutinefunction(fn):
                @wraps(fn)
                async def wrapped(*fn_args, **fn_kwargs):
                    operation_id = None
                    if StatsStore.is_capture_tool(fn.__name__):
                        operation_id = uuid.uuid4().hex
                        _stats_begin(
                            operation_id,
                            fn.__name__,
                            _safe_operation_parameters(fn, fn_args, fn_kwargs),
                        )
                    _stats_log(f"开始执行 {fn.__name__}", "start", fn.__name__)
                    try:
                        tracking_token = None
                        try:
                            if operation_id:
                                tracking_token = _TRACKED_MCP_TOOL.set(fn.__name__)
                            result = await fn(*fn_args, **fn_kwargs)
                        finally:
                            if tracking_token is not None:
                                _TRACKED_MCP_TOOL.reset(tracking_token)
                        if fn.__name__ in _COVER_REQUIRED_TOOLS:
                            result = await _ensure_video_covers(result)
                        # Persisting the operation and result is part of a
                        # successful MCP capture. If this write fails, fail
                        # the tool call instead of returning untracked data.
                        _stats_record(fn.__name__, result, operation_id)
                    except Exception as exc:
                        if operation_id:
                            _stats_fail_safely(operation_id, fn.__name__, exc)
                        _stats_log(f"执行失败 {fn.__name__}: {type(exc).__name__}", "error", fn.__name__)
                        raise
                    _stats_log(f"完成执行 {fn.__name__}", "success", fn.__name__)
                    return result
            else:
                @wraps(fn)
                def wrapped(*fn_args, **fn_kwargs):
                    operation_id = None
                    if StatsStore.is_capture_tool(fn.__name__):
                        operation_id = uuid.uuid4().hex
                        _stats_begin(
                            operation_id,
                            fn.__name__,
                            _safe_operation_parameters(fn, fn_args, fn_kwargs),
                        )
                    _stats_log(f"开始执行 {fn.__name__}", "start", fn.__name__)
                    try:
                        tracking_token = None
                        try:
                            if operation_id:
                                tracking_token = _TRACKED_MCP_TOOL.set(fn.__name__)
                            result = fn(*fn_args, **fn_kwargs)
                        finally:
                            if tracking_token is not None:
                                _TRACKED_MCP_TOOL.reset(tracking_token)
                        # Keep synchronous capture tools under the same
                        # durability contract as asynchronous tools.
                        _stats_record(fn.__name__, result, operation_id)
                    except Exception as exc:
                        if operation_id:
                            _stats_fail_safely(operation_id, fn.__name__, exc)
                        _stats_log(f"执行失败 {fn.__name__}: {type(exc).__name__}", "error", fn.__name__)
                        raise
                    _stats_log(f"完成执行 {fn.__name__}", "success", fn.__name__)
                    return result
            return parent_decorator(wrapped)

        return decorator


mcp = TrackingMCPServer("douyin-spider")


def _require_tracked_capture(tool_name: str) -> None:
    """Reject capture calls that bypass the MCP persistence wrapper."""
    if _TRACKED_MCP_TOOL.get() != tool_name:
        raise RuntimeError(
            "抓取必须通过 MCP 工具入口调用，禁止直接调用底层函数或 __wrapped__。"
        )


def _ensure_project_path() -> None:
    import sys

    root = str(AUTH.project_root)
    if root not in sys.path:
        sys.path.insert(0, root)


_ensure_project_path()


def _silent_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    # Existing project functions print response bodies; stdout belongs to MCP.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


async def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    name = getattr(fn, "__name__", "call")
    _stats_log(f"调用底层接口 {name}", "detail", name)
    try:
        # The bundled Douyin API keeps process-level request/signing state.
        # Running it in asyncio's worker pool makes identical searches return
        # an empty result on Windows. Keep API calls on the MCP event-loop
        # thread and serialize them so one shared session is not interleaved.
        async with _API_CALL_LOCK:
            result = _silent_call(fn, *args, **kwargs)
    except Exception as exc:
        _stats_log(f"底层接口失败 {name}: {type(exc).__name__}", "error", name)
        raise
    _stats_log(f"底层接口返回 {name}", "detail", name)
    return result


def _limit(value: int, low: int = 1, high: int = 200) -> int:
    return max(low, min(int(value), high))


def _digg_count(item: dict[str, Any]) -> int:
    """Return a sortable like count even when the API serializes it as text."""
    value = item.get("digg_count") or 0
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0


def _require_confirmation(confirm: bool) -> None:
    if not confirm:
        raise ValueError("这是会改变账号状态的操作，请再次调用并设置 confirm=true。")


def _api():
    _ensure_project_path()
    from dy_apis.douyin_api import DouyinAPI

    return DouyinAPI


async def _browser_work_info(work_url: str, auth: Any) -> dict[str, Any]:
    """Fetch work detail through a real Chromium session when the API is challenged."""
    from playwright.async_api import async_playwright
    from dy_apis.douyin_api import DouyinAPI

    canonical_url, _ = DouyinAPI._resolve_work_url(auth, work_url)
    bundled_browser_path = Path(__file__).resolve().parent / ".browser"
    if bundled_browser_path.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled_browser_path))

    cookies = [
        {
            "name": str(name),
            "value": str(value),
            "domain": ".douyin.com",
            "path": "/",
        }
        for name, value in auth.cookie.items()
        if value is not None and str(name).lower() not in {"domain", "path"}
    ]
    endpoint = "/aweme/v1/web/aweme/detail/"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                locale="zh-CN",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
            )
            await context.add_cookies(cookies)
            page = await context.new_page()
            try:
                async with page.expect_response(
                    lambda response: endpoint in response.url and response.status == 200,
                    timeout=45000,
                ) as response_info:
                    await page.goto(
                        canonical_url,
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                response = await response_info.value
                data = await response.json()
            except Exception as exc:
                raise RuntimeError(
                    f"浏览器回退抓取作品失败: {type(exc).__name__}: {exc}"
                ) from exc
            finally:
                await context.close()
        finally:
            await browser.close()

    if not isinstance(data, dict) or not isinstance(data.get("aweme_detail"), dict):
        raise RuntimeError("浏览器返回的数据中没有 aweme_detail，作品可能已删除或需要验证。")
    return data


async def _get_work_raw(work_url: str, auth: Any) -> dict[str, Any]:
    """Prefer the lightweight API, then transparently fall back to Chromium."""
    from dy_apis.douyin_api import DouyinAPIError

    try:
        return await _call(_api().get_work_info, auth, work_url)
    except DouyinAPIError as exc:
        _stats_log(
            f"作品接口不可用，切换浏览器回退: {type(exc).__name__}",
            "warning",
            "get_work_info",
        )
        return await _browser_work_info(work_url, auth)


@mcp.tool()
async def douyin_login(timeout_seconds: int = 180) -> dict[str, Any]:
    """打开可见浏览器，用户扫码/登录后自动保存抖音和直播 Cookie。"""
    return await AUTH.start_login(timeout_seconds)


@mcp.tool()
def douyin_login_status(session_id: str) -> dict[str, Any]:
    """查询浏览器登录会话状态，不返回 Cookie 值。"""
    return AUTH.login_status(session_id)


@mcp.tool()
def douyin_auth_status() -> dict[str, Any]:
    """检查本地登录状态和 Cookie 名称，不返回 Cookie 内容。"""
    return AUTH.status()


@mcp.tool()
def douyin_logout() -> dict[str, Any]:
    """清除本工具管理的本地抖音 Cookie。"""
    return AUTH.logout()


@mcp.tool()
async def get_work_info(work_url: str) -> dict[str, Any]:
    """获取一个视频或图集的结构化信息。"""
    _require_tracked_capture("get_work_info")
    from utils.data_util import handle_work_info

    result = await _get_work_raw(work_url, AUTH.get_auth())
    return handle_work_info(result["aweme_detail"])


@mcp.tool()
async def get_user_info(user_url: str) -> dict[str, Any]:
    """获取用户主页信息。"""
    _require_tracked_capture("get_user_info")
    return await _call(_api().get_user_info, AUTH.get_auth(), user_url)


@mcp.tool()
async def get_user_works(user_url: str, limit: int = 50) -> list[dict[str, Any]]:
    """获取用户作品，最多返回 limit 条。"""
    _require_tracked_capture("get_user_works")
    from utils.data_util import handle_work_info

    works = await _call(_api().get_user_all_work_info, AUTH.get_auth(), user_url)
    result = []
    for work in works[: _limit(limit)]:
        try:
            result.append(handle_work_info(work))
        except (KeyError, TypeError):
            result.append(work)
    return result


@mcp.tool()
async def search_videos(
    query: str,
    limit: int = 20,
    sort_type: str = "0",
    publish_time: str = "0",
    filter_duration: str = "",
    search_range: str = "0",
    content_type: str = "0",
    refresh: bool = False,
    top_liked: bool = False,
) -> list[dict[str, Any]]:
    """按关键词搜索视频；limit 是返回和落盘的硬上限。

    sort_type=1 或 top_liked=true 时，内部可以多抓候选结果用于排序，
    但最终只返回并持久化 limit 条。
    """
    _require_tracked_capture("search_videos")
    from utils.data_util import handle_work_info

    requested_limit = _limit(limit)
    rank_by_likes = bool(top_liked) or sort_type == "1"

    async def load() -> list[dict[str, Any]]:
        # Fetch a wider candidate set before slicing so "最多点赞" really
        # means the top results, even if the upstream search ordering drifts.
        fetch_limit = max(requested_limit, 50) if rank_by_likes else requested_limit
        items = await _call(
            _api().search_some_general_work,
            AUTH.get_auth(), query, fetch_limit, sort_type, publish_time,
            filter_duration, search_range, content_type,
        )
        result = []
        for item in items:
            data = item.get("aweme_info", item)
            try:
                result.append(handle_work_info(data))
            except (KeyError, TypeError):
                result.append(item)
        if rank_by_likes:
            result.sort(key=_digg_count, reverse=True)
        return result[:requested_limit]

    key = (
        "search_videos", query.strip(), requested_limit, sort_type, publish_time,
        filter_duration, search_range, content_type, rank_by_likes,
    )
    return await _cached_result(key, load, refresh)


@mcp.tool()
async def search_users(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """按关键词搜索用户。"""
    _require_tracked_capture("search_users")
    return await _call(_api().search_some_user, AUTH.get_auth(), query, _limit(limit))


@mcp.tool()
async def search_live_rooms(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """按关键词搜索直播间。"""
    _require_tracked_capture("search_live_rooms")
    return await _call(_api().search_some_live, AUTH.get_auth(), query, _limit(limit))


@mcp.tool()
async def get_comments(
    work_url: str,
    include_replies: bool = True,
    max_comments: int = 200,
    max_pages: int = 10,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """分页获取一级评论；相同请求 5 分钟内复用结果。"""
    _require_tracked_capture("get_comments")
    async def load() -> list[dict[str, Any]]:
        api = _api()
        auth = AUTH.get_auth()
        cursor = "0"
        comments: list[dict[str, Any]] = []
        for _ in range(_limit(max_pages, 1, 100)):
            page = await _call(api.get_work_out_comment, auth, work_url, cursor)
            current = page.get("comments") or []
            if not isinstance(current, list):
                break
            comments.extend(current)
            if page.get("has_more") != 1 or len(comments) >= _limit(max_comments):
                break
            cursor = str(page.get("cursor", "0"))
        comments = comments[: _limit(max_comments)]
        if include_replies:
            for comment in comments:
                if int(comment.get("reply_comment_total") or 0) > 0:
                    comment["reply_comment"] = await _call(
                        api.get_work_all_inner_comment, auth, comment
                    )
                else:
                    comment["reply_comment"] = []
        return comments

    key = (
        "get_comments", work_url.strip(), bool(include_replies),
        _limit(max_comments), _limit(max_pages, 1, 100),
    )
    return await _cached_result(key, load, refresh)


@mcp.tool()
async def get_favorite_list(limit: int = 50) -> list[dict[str, Any]]:
    """获取当前账号收藏列表。"""
    _require_tracked_capture("get_favorite_list")
    result = await _call(_api().get_collect_list, AUTH.get_auth())
    if isinstance(result, dict):
        for key in ("aweme_list", "collects", "data"):
            if isinstance(result.get(key), list):
                return result[key][:_limit(limit)]
    return result if isinstance(result, list) else [result]


@mcp.tool()
async def get_feed(limit: int = 20) -> Any:
    """获取推荐流数据。"""
    _require_tracked_capture("get_feed")
    return await _call(_api().get_feed, AUTH.get_auth(), count=str(_limit(limit)))


@mcp.tool()
async def download_work(work_url: str, save_choice: str = "all") -> dict[str, Any]:
    """下载视频/图集到原项目 datas/media_datas 目录。"""
    _require_tracked_capture("download_work")
    from utils.data_util import download_work as save_work
    from utils.data_util import handle_work_info

    if save_choice not in {"all", "media", "media-video", "media-image"}:
        raise ValueError("save_choice 必须是 all、media、media-video 或 media-image")
    raw = await _get_work_raw(work_url, AUTH.get_auth())
    work = handle_work_info(raw["aweme_detail"])
    media_root = Path(AUTH.project_root) / "datas" / "media_datas"
    path = await _call(save_work, work, str(media_root), save_choice)
    return {"success": True, "path": str(path), "work": work}


def _capture_frame(
    video_addr: str,
    output_path: Path,
    timestamp_seconds: float,
    cookies: dict[str, str] | None = None,
) -> None:
    import requests

    ffmpeg = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg")
    if not ffmpeg:
        bundled_ffmpeg = sorted(
            (Path(__file__).resolve().parent / ".browser").glob("ffmpeg-*/ffmpeg-win64.exe")
        )
        if bundled_ffmpeg:
            ffmpeg = str(bundled_ffmpeg[0])
    if not ffmpeg:
        raise RuntimeError("未找到 FFmpeg，请安装 FFmpeg、补充 .browser 文件夹，或设置 FFMPEG_PATH。")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_name = tempfile.mkstemp(suffix=".mp4")
    os.close(temp_fd)
    temp_path = Path(temp_name)
    try:
        with requests.get(
            video_addr,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151.0.0.0 Safari/537.36",
                "Referer": "https://www.douyin.com/",
                "Accept": "*/*",
            },
            cookies=cookies,
            stream=True,
            timeout=60,
        ) as response:
            response.raise_for_status()
            with temp_path.open("wb") as video_file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        video_file.write(chunk)
        command = [
            ffmpeg,
            "-y",
            "-ss",
            str(timestamp_seconds),
            "-i",
            str(temp_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output_path),
        ]
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0 or not output_path.exists():
            raise RuntimeError("视频截图失败，请确认视频仍可访问或 FFmpeg 配置正确。")
    except requests.RequestException as exc:
        raise RuntimeError("视频地址暂时无法访问，可能已经过期，请重新搜索后再截图。") from exc
    finally:
        temp_path.unlink(missing_ok=True)


@mcp.tool()
async def capture_work_frame(
    work_url: str, timestamp_seconds: float = 1.0
) -> CallToolResult:
    """截取视频指定时间点的画面，并以图片和本地文件路径返回。"""
    _require_tracked_capture("capture_work_frame")
    from utils.data_util import handle_work_info

    seconds = max(0.0, min(float(timestamp_seconds), 3600.0))
    auth = AUTH.get_auth()
    raw = await _get_work_raw(work_url, auth)
    work = handle_work_info(raw["aweme_detail"])
    video_addr = work.get("video_addr")
    if not video_addr:
        raise ValueError("这个作品没有可用的视频地址，可能是图集或视频已失效。")

    safe_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(work.get("work_id") or "work"))
    stamp = str(seconds).replace(".", "_")
    output_path = Path(
        os.getenv("DOUYIN_FRAME_OUTPUT_DIR")
        or (Path(__file__).resolve().parent / "outputs" / "frames")
    ) / f"{safe_id}_{stamp}s.png"
    await asyncio.to_thread(_capture_frame, video_addr, output_path, seconds, auth.cookie)
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    f"已截取 {seconds:g} 秒画面。文件：{output_path}\n"
                    f"视频：{work.get('work_url') or work_url}"
                ),
            ),
            Image(path=output_path).to_image_content(),
        ]
    )


@mcp.tool()
async def capture_video_frame(
    video_url: str, timestamp_seconds: float = 1.0
) -> CallToolResult:
    """对已有视频直链截取指定时间点的画面，适合接在 search_videos 后使用。"""
    _require_tracked_capture("capture_video_frame")
    seconds = max(0.0, min(float(timestamp_seconds), 3600.0))
    output_path = (
        Path(os.getenv("DOUYIN_FRAME_OUTPUT_DIR") or (Path(__file__).resolve().parent / "outputs" / "frames"))
        / f"video_{int(time.time() * 1000)}_{seconds:g}s.png"
    )
    await asyncio.to_thread(
        _capture_frame, video_url, output_path, seconds, AUTH.get_auth().cookie
    )
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=f"已截取 {seconds:g} 秒画面。文件：{output_path}",
            ),
            Image(path=output_path).to_image_content(),
        ]
    )


@mcp.tool()
async def get_live_info(live_id: str) -> dict[str, Any]:
    """获取直播间状态、房间 ID 和主播信息。"""
    _require_tracked_capture("get_live_info")
    return await _call(_api().get_live_info, AUTH.get_live_auth(), str(live_id))


@mcp.tool()
def start_live_monitor(live_id: str) -> dict[str, Any]:
    """后台启动直播监听，之后用 poll_live_events 取事件。"""
    import secrets

    monitor_id = secrets.token_urlsafe(10)
    monitor = LiveMonitor(str(live_id), AUTH.get_live_auth())
    LIVE_MONITORS[monitor_id] = monitor
    monitor.start()
    return {"monitor_id": monitor_id, "status": "starting"}


@mcp.tool()
def poll_live_events(monitor_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """读取直播弹幕、礼物、进场、点赞和关注事件。"""
    _require_tracked_capture("poll_live_events")
    monitor = LIVE_MONITORS.get(monitor_id)
    if not monitor:
        raise ValueError("直播监听会话不存在")
    return monitor.poll(limit)


@mcp.tool()
def stop_live_monitor(monitor_id: str) -> dict[str, Any]:
    """停止直播监听。"""
    monitor = LIVE_MONITORS.pop(monitor_id, None)
    if monitor:
        monitor.stop()
    return {"success": True}


@mcp.tool()
def start_private_message_monitor() -> dict[str, Any]:
    """后台启动私信监听，之后用 poll_private_messages 取消息。"""
    import secrets

    monitor_id = secrets.token_urlsafe(10)
    monitor = PrivateMonitor(AUTH.get_auth())
    PRIVATE_MONITORS[monitor_id] = monitor
    monitor.start()
    return {"monitor_id": monitor_id, "status": "starting"}


@mcp.tool()
def poll_private_messages(monitor_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """读取私信消息事件。"""
    _require_tracked_capture("poll_private_messages")
    monitor = PRIVATE_MONITORS.get(monitor_id)
    if not monitor:
        raise ValueError("私信监听会话不存在")
    return monitor.poll(limit)


@mcp.tool()
def stop_private_message_monitor(monitor_id: str) -> dict[str, Any]:
    """停止私信监听。"""
    monitor = PRIVATE_MONITORS.pop(monitor_id, None)
    if monitor:
        monitor.stop()
    return {"success": True}


@mcp.tool()
async def like_work(aweme_id: str, enabled: bool = True, confirm: bool = False) -> dict[str, Any]:
    """点赞或取消点赞，必须显式 confirm=true。"""
    _require_confirmation(confirm)
    result = await _call(_api().digg, AUTH.get_auth(), aweme_id, "1" if enabled else "0")
    return {"success": True, "enabled": enabled, "result": result}


@mcp.tool()
async def collect_work(aweme_id: str, enabled: bool = True, confirm: bool = False) -> dict[str, Any]:
    """收藏或取消收藏，必须显式 confirm=true。"""
    _require_confirmation(confirm)
    result = await _call(_api().collect_aweme, AUTH.get_auth(), aweme_id, "1" if enabled else "0")
    return {"success": True, "enabled": enabled, "result": result}


@mcp.tool()
async def publish_comment(
    aweme_id: str, content: str, reply_id: str = "", confirm: bool = False
) -> dict[str, Any]:
    """发表评论或回复，必须显式 confirm=true。"""
    _require_confirmation(confirm)
    result = await _call(_api().publish_comment, AUTH.get_auth(), aweme_id, content, reply_id)
    return {"success": True, "result": result}


@mcp.tool()
async def send_live_message(room_id: str, content: str, confirm: bool = False) -> dict[str, Any]:
    """发送直播弹幕，必须显式 confirm=true。"""
    _require_confirmation(confirm)
    result = await _call(_api().sendMsgInRoom, AUTH.get_live_auth(), str(room_id), content)
    return {"success": True, "result": result}


@mcp.tool()
async def send_private_message(user_url: str, content: str, confirm: bool = False) -> dict[str, Any]:
    """给指定用户发私信，必须显式 confirm=true。"""
    _require_confirmation(confirm)
    api = _api()
    auth = AUTH.get_auth()
    user = await _call(api.get_user_info, auth, user_url)
    user_id = user["user"]["uid"]
    conversation_id, short_id, ticket = await _call(api.create_conversation, auth, user_id)
    success = await _call(api.send_msg, auth, conversation_id, short_id, ticket, content)
    return {"success": bool(success), "recipient_uid": str(user_id)}


def main() -> None:
    _acquire_singleton_lock()
    _ensure_dashboard()
    if _SINGLETON_LOCK_OWNED:
        _stats_set_status("running")
    _stats_log("MCP 服务已启动，等待客户端调用", "start", "system")
    try:
        asyncio.run(mcp.run_stdio_async())
    finally:
        if _SINGLETON_LOCK_OWNED:
            _stats_set_status("stopped")
        _release_singleton_lock()


if __name__ == "__main__":
    main()
