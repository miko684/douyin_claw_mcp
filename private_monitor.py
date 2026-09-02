"""Background private-message monitor for MCP polling."""

from __future__ import annotations

import contextlib
import hashlib
import io
import queue
import threading
from typing import Any


class PrivateMonitor:
    app_key = "e1bd35ec9db7b8d846de66ed140b1ad9"
    fp_id = "9"

    def __init__(self, auth: Any):
        self.auth = auth
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.ws = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        def runner() -> None:
            try:
                from websocket import WebSocketApp
                from builder.header import HeaderBuilder
                from builder.params import Params
                from dy_apis.douyin_api import DouyinAPI

                device_id = DouyinAPI.get_device_id(self.auth)
                access_key = hashlib.md5(
                    f"{self.fp_id + self.app_key + device_id}f8a69f1719916z".encode()
                ).hexdigest()
                params = Params()
                (params.add_param("aid", "6383")
                 .add_param("device_platform", "douyin_pc")
                 .add_param("fpid", self.fp_id)
                 .add_param("device_id", device_id)
                 .add_param("token", self.auth.cookie["sessionid"])
                 .add_param("access_key", access_key))
                url = f"wss://frontier-im.douyin.com/ws/v2?{params.toString()}"

                def on_message(ws, raw):
                    self._parse(raw)

                def on_open(ws):
                    self.events.put({"type": "status", "status": "connected"})

                def on_error(ws, error):
                    self.events.put({"type": "error", "message": str(error)})

                def on_close(ws, code, msg):
                    self.events.put(
                        {"type": "status", "status": "closed", "code": code, "message": msg}
                    )

                with contextlib.redirect_stdout(io.StringIO()):
                    self.ws = WebSocketApp(
                        url=url,
                        header={"User-Agent": HeaderBuilder.ua,
                                "Sec-WebSocket-Protocol": "binary, base64, pbbp2"},
                        cookie=self.auth.cookie_str,
                        on_message=on_message, on_open=on_open,
                        on_error=on_error, on_close=on_close,
                    )
                    self.ws.run_forever(origin="https://www.douyin.com")
            except Exception as exc:
                self.events.put(
                    {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
                )

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()

    def _parse(self, raw: bytes) -> None:
        import json
        from static import Live_pb2, Response_pb2

        try:
            frame = Live_pb2.PushFrame()
            frame.ParseFromString(raw)
            if frame.payloadType == "text/json":
                self.events.put({"type": "json", "data": json.loads(frame.payload)})
                return
            if frame.payloadType != "pb":
                return
            response = Response_pb2.Response()
            response.ParseFromString(frame.payload)
            message = response.body.new_message_notify.message
            content = json.loads(message.content)
            self.events.put(
                {"type": "private_message", "sender": str(message.sender),
                 "message_type": message.message_type,
                 "conversation_id": message.conversation_id,
                 "index": message.index_in_conversation, "content": content}
            )
        except Exception as exc:
            self.events.put({"type": "error", "message": str(exc)})

    def poll(self, limit: int = 50) -> list[dict[str, Any]]:
        result = []
        for _ in range(max(1, min(int(limit), 200))):
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                break
        return result

    def stop(self) -> None:
        if self.ws is not None:
            self.ws.close()
