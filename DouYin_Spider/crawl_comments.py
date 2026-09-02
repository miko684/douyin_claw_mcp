# coding=utf-8
"""慢速抓取单个抖音作品的一级评论和二级回复。"""

import json
import random
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from openpyxl import Workbook

from dy_apis.douyin_api import DouyinAPI
from utils.common_util import init


SOURCE_URL = "https://v.douyin.com/r-SW63z2ZpA/"
MIN_DELAY = 8
MAX_DELAY = 15


_requests_get = requests.get


def requests_get_with_timeout(*args, **kwargs):
    kwargs.setdefault("timeout", 30)
    return _requests_get(*args, **kwargs)


# 项目原有接口没有统一设置超时；为避免抓取无限挂起，给本脚本内请求补上超时。
requests.get = requests_get_with_timeout


def pause(api_calls: int) -> None:
    """请求之间随机暂停；每十次请求额外冷却。"""
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    if api_calls and api_calls % 10 == 0:
        time.sleep(random.uniform(15, 25))


def resolve_aweme_url(url: str) -> tuple[str, str]:
    response = requests.get(
        url,
        allow_redirects=True,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    match = re.search(r"/(?:video|note)/(\d+)", response.url)
    if not match:
        match = re.search(r"[?&]modal_id=(\d+)", response.url)
    if not match:
        raise RuntimeError(f"无法从跳转地址解析作品 ID: {response.url}")
    aweme_id = match.group(1)
    return aweme_id, f"https://www.douyin.com/video/{aweme_id}"


def crawl_comments(auth, work_url: str) -> list[dict]:
    comments: list[dict] = []
    cursor = "0"
    api_calls = 0
    page = 0
    reply_failed = False

    while True:
        if api_calls:
            pause(api_calls)
        page += 1
        result = None
        for attempt in range(1, 4):
            try:
                result = DouyinAPI.get_work_out_comment(auth, work_url, cursor)
                break
            except Exception as exc:
                if attempt == 3:
                    print(
                        f"一级评论接口连续失败，停止翻页：{type(exc).__name__}",
                        flush=True,
                    )
                    break
                print(
                    f"一级评论请求失败，第 {attempt} 次重试前等待 20-30 秒："
                    f"{type(exc).__name__}",
                    flush=True,
                )
                time.sleep(random.uniform(20, 30))
        if result is None:
            break
        api_calls += 1
        current = result.get("comments") or []
        print(f"一级评论第 {page} 页：{len(current)} 条，累计请求 {api_calls} 次", flush=True)

        if not isinstance(current, list):
            break

        for original in current:
            comment = dict(original)
            comment["reply_comment"] = []
            reply_total = int(comment.get("reply_comment_total") or 0)
            if reply_total > 0 and not reply_failed:
                reply_cursor = "0"
                reply_page = 0
                while True:
                    pause(api_calls)
                    reply_page += 1
                    try:
                        replies = DouyinAPI.get_work_inner_comment(
                            auth, comment, reply_cursor, count="5"
                        )
                    except Exception as exc:
                        reply_failed = True
                        print(f"  二级回复接口暂时不可用，跳过后续回复：{type(exc).__name__}", flush=True)
                        break
                    api_calls += 1
                    reply_items = replies.get("comments") or []
                    if isinstance(reply_items, list):
                        comment["reply_comment"].extend(reply_items)
                    print(
                        f"  评论 {comment.get('cid', '')} 回复第 {reply_page} 页："
                        f"{len(reply_items) if isinstance(reply_items, list) else 0} 条"
                        , flush=True
                    )
                    if replies.get("has_more") != 1:
                        break
                    reply_cursor = str(replies.get("cursor", "0"))
            comments.append(comment)

        if result.get("has_more") != 1:
            break
        cursor = str(result.get("cursor", "0"))

    return comments


def save_results(comments: list[dict], aweme_id: str) -> tuple[Path, Path]:
    desktop = Path.home() / "Desktop"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = desktop / f"douyin_comments_{aweme_id}_{stamp}.json"
    xlsx_path = desktop / f"douyin_comments_{aweme_id}_{stamp}.xlsx"

    json_path.write_text(
        json.dumps(comments, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "comments"
    sheet.append(["评论层级", "评论ID", "用户昵称", "评论内容", "点赞数", "发布时间"])
    for comment in comments:
        user = comment.get("user") or {}
        sheet.append(
            [
                "一级",
                comment.get("cid", ""),
                user.get("nickname", ""),
                comment.get("text", ""),
                comment.get("digg_count", 0),
                comment.get("create_time", ""),
            ]
        )
        for reply in comment.get("reply_comment") or []:
            reply_user = reply.get("user") or {}
            sheet.append(
                [
                    "二级回复",
                    reply.get("cid", ""),
                    reply_user.get("nickname", ""),
                    reply.get("text", ""),
                    reply.get("digg_count", 0),
                    reply.get("create_time", ""),
                ]
            )
    workbook.save(xlsx_path)
    return json_path, xlsx_path


if __name__ == "__main__":
    auth, _ = init()
    if not (auth.cookie or {}).get("ttwid"):
        raise RuntimeError("未读取到有效 DY_COOKIES，请检查 .env")
    aweme_id, work_url = resolve_aweme_url(SOURCE_URL)
    print(f"作品 ID: {aweme_id}", flush=True)
    print(f"慢速抓取开始，单次请求间隔约 {MIN_DELAY}-{MAX_DELAY} 秒", flush=True)
    result = crawl_comments(auth, work_url)
    json_file, xlsx_file = save_results(result, aweme_id)
    print(f"完成：一级评论 {len(result)} 条", flush=True)
    print(f"JSON: {json_file}", flush=True)
    print(f"Excel: {xlsx_file}", flush=True)
