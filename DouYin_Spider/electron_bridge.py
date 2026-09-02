# coding=utf-8
"""Electron 与原有抖音采集代码之间的本地 JSONL 桥接进程。"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, set_key
from loguru import logger

from builder.auth import DouyinAuth
from main import Data_Spider


OUTPUT_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
AUTH: DouyinAuth | None = None
DATA_DIR: Path
ENV_FILE: Path
BUSY = False


def emit(payload: dict[str, Any]) -> None:
    """向 Electron 输出一行 JSON；绝不输出 Cookie 内容。"""
    with OUTPUT_LOCK:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def log_to_electron(message: Any) -> None:
    text = str(message).strip()
    if text:
        emit({"type": "event", "event": "log", "message": text})


logger.remove()
logger.add(log_to_electron, level="INFO")


def normalize_cookies(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        result: dict[str, str] = {}
        for item in value:
            if isinstance(item, dict) and item.get("name"):
                result[str(item["name"])] = str(item.get("value", ""))
        return result
    return {}


def auth_from_browser_state(state: dict[str, Any]) -> DouyinAuth:
    cookies = normalize_cookies(state.get("cookies"))
    web_protect = str(state.get("webProtect") or "")
    keys = str(state.get("keys") or "")
    if not cookies:
        raise ValueError("没有读取到抖音会话 Cookie，请保持登录窗口打开后重试")
    if not web_protect or not keys:
        raise ValueError("登录页面还没有准备好安全凭证，请等待页面加载后重试")

    auth = DouyinAuth()
    auth.perepare_auth("", web_protect, keys)
    auth.cookie = cookies
    auth._ttwid = cookies.get("ttwid", "")
    auth.cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return auth


def auth_from_env() -> DouyinAuth | None:
    if not ENV_FILE.exists():
        return None
    values = dotenv_values(ENV_FILE)
    cookies = values.get("DY_COOKIES") or ""
    ticket = values.get("DY_TICKET") or ""
    private_key = values.get("DY_PRIVATE_KEY") or ""
    if not cookies or not ticket or not private_key:
        return None
    auth = DouyinAuth()
    auth.perepare_auth(cookies, "", "")
    auth.ticket = ticket
    auth.ts_sign = values.get("DY_TS_SIGN") or None
    auth.client_cert = values.get("DY_CLIENT_CERT") or None
    auth.private_key = private_key
    return auth


def save_auth(auth: DouyinAuth) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    values = {
        "DY_COOKIES": "; ".join(f"{k}={v}" for k, v in (auth.cookie or {}).items()),
        "DY_TICKET": auth.ticket or "",
        "DY_TS_SIGN": auth.ts_sign or "",
        "DY_CLIENT_CERT": auth.client_cert or "",
        "DY_PRIVATE_KEY": auth.private_key or "",
    }
    for key, value in values.items():
        if value:
            set_key(str(ENV_FILE), key, value, quote_mode="auto")


def safe_auth_summary(auth: DouyinAuth | None) -> dict[str, Any]:
    if auth is None:
        return {"authenticated": False}
    cookies = auth.cookie or {}
    has_session = bool(cookies.get("sessionid") or cookies.get("sessionid_ss"))
    return {
        "authenticated": bool(auth.ticket and auth.private_key and has_session),
        "cookieCount": len(cookies),
        "hasSession": has_session,
        "uid": auth.uid,
    }


def ensure_auth() -> DouyinAuth:
    global AUTH
    with STATE_LOCK:
        if AUTH is None:
            AUTH = auth_from_env()
        if AUTH is None:
            raise RuntimeError("请先扫码登录抖音")
        return AUTH


def handle_login_state(request: dict[str, Any]) -> dict[str, Any]:
    global AUTH
    auth = auth_from_browser_state(request)
    summary = safe_auth_summary(auth)
    if not summary["hasSession"]:
        raise ValueError("暂未检测到登录会话，请在抖音登录窗口完成扫码并确认登录")
    with STATE_LOCK:
        AUTH = auth
    save_auth(auth)
    return {"authenticated": True, "cookieCount": summary["cookieCount"]}


def create_base_path() -> dict[str, str]:
    media = DATA_DIR / "media_datas"
    excel = DATA_DIR / "excel_datas"
    media.mkdir(parents=True, exist_ok=True)
    excel.mkdir(parents=True, exist_ok=True)
    return {"media": str(media), "excel": str(excel)}


def handle_crawl(request: dict[str, Any]) -> dict[str, Any]:
    auth = ensure_auth()
    base_path = create_base_path()
    mode = request.get("mode", "search")
    save_choice = request.get("saveChoice", "excel")
    if save_choice not in {"excel", "media", "media-video", "media-image", "all"}:
        save_choice = "excel"
    spider = Data_Spider()

    if mode == "search":
        query = str(request.get("query") or "").strip()
        if not query:
            raise ValueError("请输入搜索关键词")
        require_num = max(1, min(int(request.get("requireNum") or 20), 100))
        spider.spider_some_search_work(
            auth, query, require_num, base_path, save_choice,
            str(request.get("sortType") or "0"), str(request.get("publishTime") or "0"),
            str(request.get("filterDuration") or ""), str(request.get("searchRange") or "0"),
            str(request.get("contentType") or "0"),
        )
        target = f"搜索：{query}"
    elif mode == "work":
        url = str(request.get("url") or "").strip()
        if not url:
            raise ValueError("请输入作品链接")
        spider.spider_some_work(auth, [url], base_path, save_choice, "work")
        target = "作品详情"
    elif mode == "user":
        url = str(request.get("url") or "").strip()
        if not url:
            raise ValueError("请输入用户主页链接")
        spider.spider_user_all_work(auth, url, base_path, save_choice)
        target = "用户全部作品"
    else:
        raise ValueError(f"不支持的抓取类型：{mode}")

    return {"message": f"{target}抓取完成", "outputDir": str(DATA_DIR)}


def process_request(request: dict[str, Any]) -> None:
    global BUSY, AUTH
    request_id = request.get("id")
    try:
        command = request.get("command")
        if command == "ping":
            data = {"ready": True}
        elif command == "load_saved":
            with STATE_LOCK:
                AUTH = auth_from_env()
            data = safe_auth_summary(AUTH)
        elif command == "login_state":
            data = handle_login_state(request)
        elif command == "crawl":
            data = handle_crawl(request)
        else:
            raise ValueError(f"未知命令：{command}")
        emit({"type": "response", "id": request_id, "ok": True, "data": data})
    except Exception as exc:
        logger.error(f"操作失败：{exc}")
        emit({"type": "response", "id": request_id, "ok": False, "error": str(exc), "detail": traceback.format_exc(limit=3)})
    finally:
        if request.get("command") == "crawl":
            with STATE_LOCK:
                BUSY = False


def main() -> None:
    global DATA_DIR, ENV_FILE
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    DATA_DIR = Path(args.data_dir).resolve()
    ENV_FILE = DATA_DIR / ".env"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    emit({"type": "ready", "dataDir": str(DATA_DIR)})

    for raw_line in sys.stdin:
        if not raw_line.strip():
            continue
        try:
            request = json.loads(raw_line)
        except json.JSONDecodeError:
            emit({"type": "event", "event": "log", "message": "收到无法解析的请求"})
            continue
        if request.get("command") == "crawl":
            with STATE_LOCK:
                if BUSY:
                    emit({"type": "response", "id": request.get("id"), "ok": False, "error": "已有抓取任务正在运行"})
                    continue
                BUSY = True
            threading.Thread(target=process_request, args=(request,), daemon=True).start()
        else:
            process_request(request)


if __name__ == "__main__":
    main()
