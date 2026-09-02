"""Small local metrics store shared by the MCP server and dashboard."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class StatsStore:
    _CAPTURE_TOOL_KINDS = {
        "get_work_info": "videos",
        "get_user_info": "other",
        "get_user_works": "videos",
        "search_videos": "videos",
        "search_users": "other",
        "search_live_rooms": "live_rooms",
        "get_comments": "comments",
        "get_favorite_list": "videos",
        "get_feed": "videos",
        "download_work": "videos",
        "capture_work_frame": "videos",
        "capture_video_frame": "videos",
        "get_live_info": "live_rooms",
        "poll_live_events": "live_messages",
        "poll_private_messages": "private_messages",
    }

    def __init__(self, path: str | None = None):
        default = Path(__file__).resolve().parent / "work" / "mcp_stats.json"
        self.path = Path(path or os.getenv("DOUYIN_STATS_FILE") or default)
        self._lock = threading.Lock()

    def _empty(self) -> dict[str, Any]:
        return {
            "status": "stopped",
            "started_at": None,
            "updated_at": int(time.time()),
            "counts": {
                "tool_calls": 0,
                "videos": 0,
                "comments": 0,
                "live_rooms": 0,
                "live_messages": 0,
                "private_messages": 0,
            },
            "logs": [],
            "artifacts": [],
            "operations": [],
            "recent": [],
            "seen": {
                "videos": [],
                "comments": [],
                "live_rooms": [],
                "live_messages": [],
                "private_messages": [],
            },
        }

    def read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return self._empty()

    def write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Use a process-specific temporary file so a dashboard/MCP restart
        # cannot overwrite another writer's temp file on Windows.
        temp = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            for attempt in range(6):
                try:
                    temp.replace(self.path)
                    return
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            temp.unlink(missing_ok=True)

    def set_status(self, status: str) -> None:
        with self._lock:
            data = self.read()
            data["status"] = status
            data["updated_at"] = int(time.time())
            if status == "running" and not data.get("started_at"):
                data["started_at"] = int(time.time())
            self.write(data)

    def log(self, message: str, level: str = "info", tool: str = "") -> None:
        with self._lock:
            data = self.read()
            logs = data.setdefault("logs", [])
            logs.insert(
                0,
                {
                    "at": int(time.time()),
                    "level": level,
                    "tool": tool,
                    "message": message,
                },
            )
            del logs[120:]
            data["updated_at"] = int(time.time())
            self.write(data)

    @classmethod
    def is_capture_tool(cls, tool_name: str) -> bool:
        """Return whether a tool performs a data/media collection operation."""
        return tool_name in cls._CAPTURE_TOOL_KINDS

    @classmethod
    def _operation_kind(cls, tool_name: str) -> str:
        return cls._CAPTURE_TOOL_KINDS.get(tool_name, "other")

    @staticmethod
    def _result_count(result: Any) -> int:
        if isinstance(result, list):
            return len(result)
        if isinstance(result, dict):
            for key in (
                "aweme_list", "comments", "rooms", "live_rooms", "events",
                "messages", "users", "data", "items",
            ):
                value = result.get(key)
                if isinstance(value, list):
                    return len(value)
            return 1 if result else 0
        return 1 if result is not None else 0

    def begin_operation(
        self,
        operation_id: str,
        tool_name: str,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        """Persist a collection attempt before any network work starts."""
        if not self.is_capture_tool(tool_name):
            return
        with self._lock:
            data = self.read()
            now = int(time.time())
            counts = data.setdefault("counts", self._empty()["counts"])
            counts["tool_calls"] = int(counts.get("tool_calls", 0)) + 1
            recent = data.setdefault("recent", [])
            recent.insert(0, {"tool": tool_name, "at": now, "operation_id": operation_id})
            del recent[20:]
            operations = data.setdefault("operations", [])
            operations.insert(
                0,
                {
                    "id": operation_id,
                    "tool": tool_name,
                    "kind": self._operation_kind(tool_name),
                    "status": "running",
                    "started_at": now,
                    "finished_at": None,
                    "count": 0,
                    "parameters": parameters or {},
                },
            )
            del operations[100:]
            data["updated_at"] = now
            self.write(data)

    @staticmethod
    def _find_operation(data: dict[str, Any], operation_id: str) -> dict[str, Any] | None:
        for operation in data.setdefault("operations", []):
            if operation.get("id") == operation_id:
                return operation
        return None

    def fail_operation(self, operation_id: str, tool_name: str, error: str) -> None:
        """Finish an already-persisted attempt as failed without losing it."""
        if not self.is_capture_tool(tool_name):
            return
        with self._lock:
            data = self.read()
            now = int(time.time())
            operation = self._find_operation(data, operation_id)
            if operation is None:
                operation = {
                    "id": operation_id,
                    "tool": tool_name,
                    "kind": self._operation_kind(tool_name),
                    "started_at": now,
                    "parameters": {},
                }
                data.setdefault("operations", []).insert(0, operation)
            operation.update(
                {
                    "status": "failed",
                    "finished_at": now,
                    "count": 0,
                    "error": error[:300],
                }
            )
            del data.setdefault("operations", [])[100:]
            data["updated_at"] = now
            self.write(data)

    @staticmethod
    def _items(result: Any, keys: tuple[str, ...]) -> list[Any]:
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in keys:
                value = result.get(key)
                if isinstance(value, list):
                    return value
            return [result]
        return []

    @staticmethod
    def _item_key(kind: str, item: Any) -> str:
        """Return a stable identity so repeated MCP results are idempotent."""
        if isinstance(item, dict):
            if kind == "videos":
                value = item.get("work_id") or item.get("aweme_id") or item.get("work_url")
            elif kind == "comments":
                value = item.get("cid") or item.get("comment_id")
                if not value:
                    value = "|".join(
                        str(item.get(field, ""))
                        for field in ("aweme_id", "create_time", "text")
                    )
            elif kind == "live_rooms":
                value = item.get("room_id") or item.get("live_id") or item.get("id")
            else:
                value = item.get("event_id") or item.get("id")
            if value:
                return f"{kind}:{value}"
        return f"{kind}:raw:{json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)}"

    def _new_items(self, data: dict[str, Any], kind: str, items: list[Any]) -> tuple[list[Any], list[str]]:
        """Filter items already recorded in this local collection."""
        seen = data.setdefault("seen", {})
        known = [str(value) for value in seen.setdefault(kind, [])]
        known_set = set(known)

        # Older stats files did not have ``seen``. Recover identities from
        # their artifact previews so upgrading does not re-count old data.
        for artifact in data.get("artifacts", []):
            if artifact.get("kind") != kind:
                continue
            for value in artifact.get("item_keys", []):
                if value not in known_set:
                    known.append(str(value))
                    known_set.add(str(value))
            for item in artifact.get("items", []):
                key = self._item_key(kind, item)
                if key not in known_set:
                    known.append(key)
                    known_set.add(key)

        fresh: list[Any] = []
        fresh_keys: list[str] = []
        for item in items:
            key = self._item_key(kind, item)
            if key in known_set:
                continue
            known.append(key)
            known_set.add(key)
            fresh.append(item)
            fresh_keys.append(key)
        seen[kind] = known
        return fresh, fresh_keys

    def _save_artifact(
        self,
        kind: str,
        tool_name: str,
        result: Any,
        items: list[Any],
        item_keys: list[str],
    ) -> dict[str, Any] | None:
        if hasattr(result, "content") or not items:
            return None

        payload: Any = items
        if isinstance(result, dict):
            keys = {
                "videos": ("aweme_list", "data", "items"),
                "comments": ("comments", "data", "items"),
                "live_rooms": ("rooms", "live_rooms", "data", "items"),
            }.get(kind, ("events", "messages", "data", "items"))
            for key in keys:
                if isinstance(result.get(key), list):
                    payload = dict(result)
                    payload[key] = items
                    break
            else:
                payload = items[0] if len(items) == 1 else items

        results_root = self.path.parent / "results"
        results_root.mkdir(parents=True, exist_ok=True)
        filename = f"{kind}_{int(time.time())}_{uuid.uuid4().hex[:8]}.json"
        path = results_root / filename
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return {
            "kind": kind,
            "tool": tool_name,
            "count": len(items),
            "path": f"results/{filename}",
            "at": int(time.time()),
            "items": items[:50],
            "item_keys": item_keys,
        }

    def _replace_kind(self, data: dict[str, Any], kind: str) -> None:
        """Replace a snapshot collection instead of accumulating old results."""
        results_root = self.path.parent / "results"
        paths: set[Path] = set()
        for artifact in data.get("artifacts", []):
            if artifact.get("kind") != kind:
                continue
            relative = Path(str(artifact.get("path", "")))
            target = (self.path.parent / relative).resolve()
            if target.parent == results_root.resolve() and target.suffix.lower() == ".json":
                paths.add(target)
        if results_root.is_dir():
            paths.update(results_root.glob(f"{kind}_*.json"))
        for path in paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

        data["artifacts"] = [
            artifact for artifact in data.get("artifacts", [])
            if artifact.get("kind") != kind
        ]
        data.setdefault("seen", {})[kind] = []
        data.setdefault("counts", self._empty()["counts"])[kind] = 0

    def record(
        self,
        tool_name: str,
        result: Any = None,
        operation_id: str | None = None,
    ) -> None:
        with self._lock:
            data = self.read()
            counts = data.setdefault("counts", self._empty()["counts"])
            now = int(time.time())
            if operation_id is None:
                # Backwards-compatible path for non-collection tools and
                # callers that still record only after successful return.
                counts["tool_calls"] = int(counts.get("tool_calls", 0)) + 1
                recent = data.setdefault("recent", [])
                recent.insert(0, {"tool": tool_name, "at": now})
                del recent[20:]
            else:
                operation = self._find_operation(data, operation_id)
                if operation is None:
                    operation = {
                        "id": operation_id,
                        "tool": tool_name,
                        "kind": self._operation_kind(tool_name),
                        "started_at": now,
                        "parameters": {},
                    }
                    data.setdefault("operations", []).insert(0, operation)
                result_count = self._result_count(result)
                operation.update(
                    {
                        "status": "success" if result_count else "empty",
                        "finished_at": now,
                        "count": result_count,
                    }
                )
                operation.pop("error", None)
                del data.setdefault("operations", [])[100:]
            kind = {
                "get_work_info": "videos",
                "get_user_works": "videos",
                "search_videos": "videos",
                "get_feed": "videos",
                "get_comments": "comments",
                "get_live_info": "live_rooms",
                "search_live_rooms": "live_rooms",
                "poll_live_events": "live_messages",
                "poll_private_messages": "private_messages",
            }.get(tool_name)
            if kind:
                # Search results are a snapshot. A new search must not leave
                # stale candidates from an earlier, wider ranking query in
                # the dashboard or result files.
                if tool_name == "search_videos":
                    self._replace_kind(data, kind)
                if kind == "videos":
                    items = self._items(result, ("aweme_list", "data", "items"))
                elif kind == "comments":
                    items = self._items(result, ("comments", "data", "items"))
                elif kind == "live_rooms":
                    items = self._items(result, ("rooms", "live_rooms", "data", "items"))
                else:
                    items = self._items(result, ("events", "messages", "data", "items"))
                if items:
                    fresh_items, fresh_keys = self._new_items(data, kind, items)
                    if fresh_items:
                        counts[kind] += len(fresh_items)
                        artifact = self._save_artifact(
                            kind, tool_name, result, fresh_items, fresh_keys
                        )
                        if artifact:
                            artifacts = data.setdefault("artifacts", [])
                            artifacts.insert(0, artifact)
                            del artifacts[80:]
                            if operation_id:
                                operation = self._find_operation(data, operation_id)
                                if operation is not None:
                                    operation["artifact_path"] = artifact["path"]
            data["updated_at"] = now
            self.write(data)

    def clear(self, kind: str | None = None) -> dict[str, Any]:
        """Clear collected result data without touching the login session."""
        kind_map = {
            "videos": {"videos"},
            "comments": {"comments"},
            "live_rooms": {"live_rooms"},
            # The dashboard groups live events and private messages together.
            "live_messages": {"live_messages", "private_messages"},
        }
        if kind is not None and kind not in kind_map:
            raise ValueError(f"unsupported clear kind: {kind}")

        with self._lock:
            data = self.read()
            artifacts = data.setdefault("artifacts", [])
            results_root = self.path.parent / "results"
            paths: set[Path] = set()
            if kind is None:
                if results_root.is_dir():
                    paths.update(results_root.glob("*.json"))
                cleared_kinds = set().union(*kind_map.values())
            else:
                cleared_kinds = kind_map[kind]
                for artifact in artifacts:
                    if artifact.get("kind") not in cleared_kinds:
                        continue
                    relative = Path(str(artifact.get("path", "")))
                    target = (self.path.parent / relative).resolve()
                    if target.parent == results_root.resolve() and target.suffix.lower() == ".json":
                        paths.add(target)
                if results_root.is_dir():
                    for artifact_kind in cleared_kinds:
                        paths.update(results_root.glob(f"{artifact_kind}_*.json"))

            deleted = 0
            failed: list[str] = []
            for path in paths:
                try:
                    path.unlink()
                    deleted += 1
                except FileNotFoundError:
                    pass
                except OSError:
                    failed.append(str(path))

            counts = data.setdefault("counts", self._empty()["counts"])
            seen = data.setdefault("seen", {})
            if kind is None:
                for key in self._empty()["counts"]:
                    counts[key] = 0
                artifacts.clear()
                data["operations"] = []
                data["recent"] = []
                seen.clear()
                message = "已清空全部采集数据（登录 Cookie 保留）"
            else:
                if kind == "videos":
                    counts["videos"] = 0
                elif kind == "comments":
                    counts["comments"] = 0
                elif kind == "live_rooms":
                    counts["live_rooms"] = 0
                else:
                    counts["live_messages"] = 0
                    counts["private_messages"] = 0
                for cleared_kind in cleared_kinds:
                    seen.pop(cleared_kind, None)
                data["artifacts"] = [
                    artifact for artifact in artifacts
                    if artifact.get("kind") not in cleared_kinds
                ]
                data["operations"] = [
                    operation for operation in data.get("operations", [])
                    if operation.get("kind") not in cleared_kinds
                ]
                data["recent"] = [
                    event for event in data.get("recent", [])
                    if self._tool_kind(event.get("tool")) not in cleared_kinds
                ]
                message = f"已清空{kind}板块数据（登录 Cookie 保留）"

            logs = data.setdefault("logs", [])
            logs.insert(0, {"at": int(time.time()), "level": "info", "tool": "system", "message": message})
            del logs[120:]
            data["updated_at"] = int(time.time())
            self.write(data)
            return {"kind": kind or "all", "deleted_files": deleted, "failed_files": failed}

    @staticmethod
    def _tool_kind(tool_name: str | None) -> str | None:
        return {
            "get_work_info": "videos",
            "get_user_works": "videos",
            "search_videos": "videos",
            "get_feed": "videos",
            "get_comments": "comments",
            "get_live_info": "live_rooms",
            "search_live_rooms": "live_rooms",
            "start_live_monitor": "live_rooms",
            "poll_live_events": "live_messages",
            "poll_private_messages": "private_messages",
        }.get(tool_name or "")
