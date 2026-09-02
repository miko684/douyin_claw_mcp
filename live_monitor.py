"""MCP-friendly live monitor: background WebSocket plus polling."""

from __future__ import annotations

import contextlib
import io
import queue
import threading
from typing import Any


class LiveMonitor:
    def __init__(self, live_id: str, auth: Any):
        self.live_id = str(live_id)
        self.auth = auth
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.ws = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        def runner() -> None:
            try:
                from dy_live.server import DouyinLive

                owner = self

                class CapturingLive(DouyinLive):
                    def on_open(self, ws):
                        owner.events.put({"type": "status", "status": "connected"})
                        threading.Thread(
                            target=self.ping, args=(ws,), daemon=True
                        ).start()

                    def on_message(self, ws, message):
                        owner._parse_message(self, message)

                    def on_error(self, ws, error):
                        owner.events.put({"type": "error", "message": str(error)})

                    def on_close(self, ws, code, msg):
                        owner.events.put(
                            {"type": "status", "status": "closed", "code": code, "message": msg}
                        )

                with contextlib.redirect_stdout(io.StringIO()):
                    instance = CapturingLive(self.live_id, self.auth)
                    self.ws = instance
                    instance.start_ws()
            except Exception as exc:
                self.events.put(
                    {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
                )

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()

    def _parse_message(self, live_instance: Any, raw_message: bytes) -> None:
        import gzip
        import static.Live_pb2 as Live_pb2

        try:
            frame = Live_pb2.PushFrame()
            frame.ParseFromString(raw_message)
            response = Live_pb2.LiveResponse()
            response.ParseFromString(gzip.decompress(frame.payload))
            if response.needAck:
                ack = Live_pb2.PushFrame()
                ack.payloadType = "ack"
                ack.payload = response.internalExt.encode("utf-8")
                ack.logId = frame.logId
                live_instance.ws.send(ack.SerializeToString(), opcode=0x02)
            for item in response.messagesList:
                event: dict[str, Any] = {"method": item.method}
                if item.method == "WebcastGiftMessage":
                    obj = Live_pb2.GiftMessage()
                    obj.ParseFromString(item.payload)
                    event.update(
                        {"type": "gift", "user": obj.user.nickname,
                         "to_user": obj.toUser.nickname, "gift": obj.gift.name,
                         "count": obj.comboCount}
                    )
                elif item.method == "WebcastChatMessage":
                    obj = Live_pb2.ChatMessage()
                    obj.ParseFromString(item.payload)
                    event.update(
                        {"type": "chat", "user": obj.user.nickname,
                         "content": obj.content}
                    )
                elif item.method == "WebcastMemberMessage":
                    obj = Live_pb2.MemberMessage()
                    obj.ParseFromString(item.payload)
                    event.update({"type": "member", "user": obj.user.nickname})
                elif item.method == "WebcastLikeMessage":
                    obj = Live_pb2.LikeMessage()
                    obj.ParseFromString(item.payload)
                    event.update(
                        {"type": "like", "user": obj.user.nickname,
                         "count": obj.count, "total": obj.total}
                    )
                elif item.method == "WebcastSocialMessage":
                    obj = Live_pb2.SocialMessage()
                    obj.ParseFromString(item.payload)
                    event.update({"type": "follow", "user": obj.user.nickname})
                elif item.method == "WebcastRoomStatsMessage":
                    obj = Live_pb2.RoomStatsMessage()
                    obj.ParseFromString(item.payload)
                    event.update({"type": "stats", "display": obj.displayLong})
                else:
                    event["type"] = "other"
                self.events.put(event)
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
        if self.ws is not None and getattr(self.ws, "ws", None) is not None:
            self.ws.ws.close()
