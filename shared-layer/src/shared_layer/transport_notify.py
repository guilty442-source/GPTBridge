"""Transport LISTEN/NOTIFY 消費端（§10.63／G26／INT-4：輪詢改事件）。

生產端已存在：`PostgresSharedLayerStore` 對新 `tool_request` 列以
``pg_notify('tool_request_<channel_id>', request_id)`` 公告（store.py）。
本模組提供消費端：一條**專用連線**（不佔 lane pool 配額，見
``ConnectionManager.dedicated_connection``）LISTEN 全部已訂閱 channel，
把 request_id 分派給訂閱回呼。

設計邊界：
- 通知只是**喚醒提示**——丟失／斷線由訂閱方的週期輪詢兜底（fail-closed
  fallback）；監聽器本身重連退避，絕不讓輪詢失效。
- callback 在監聽執行緒執行——訂閱方必須自行轉入自己的事件迴圈
  （如 ``loop.call_soon_threadsafe``）。
- lazy：無訂閱者不開連線；``subscribe`` 後自動（重）LISTEN。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
)
from typing import Any, Callable

_logger = logging.getLogger("gptbridge.transport_notify")

_CHANNEL_RE = re.compile(r"^[a-z0-9_]+$")

_NOTIFY_CHANNEL_PREFIX = "tool_request_"


class TransportNotifyListener:
    """LISTEN ``tool_request_<channel>``；payload → subscriber callback。"""

    def __init__(
        self,
        *,
        manager_factory: Callable[[], Any] | None = None,
        min_backoff_s: float = 1.0,
        max_backoff_s: float = 60.0,
        poll_slice_s: float = 0.5,
    ) -> None:
        self._manager_factory = manager_factory
        self._min_backoff = max(0.1, float(min_backoff_s))
        self._max_backoff = max(self._min_backoff, float(max_backoff_s))
        self._poll_slice = max(0.1, float(poll_slice_s))
        self._subscribers: dict[str, list[Callable[[str, str], None]]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._resubscribe = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error = ""
        # P7: subscriber callbacks are contractually cheap re-dispatchers
        # (call_soon_threadsafe), but the contract was unenforced — one
        # wedged callback stalled every channel.  Dispatch through a
        # bounded 2-worker executor with a per-callback deadline; wedged
        # workers are never joined (leak bounded at 2 threads total) and
        # stalls are counted.  Lazily created so stop()/start() cycles
        # always get a live pool.
        self._dispatch_executor: ThreadPoolExecutor | None = None
        self._dispatch_stalls = 0
        self._dispatch_inflight = 0

    # -- subscription ---------------------------------------------------

    def subscribe(
        self, channel_id: str, callback: Callable[[str, str], None]
    ) -> None:
        """訂閱 ``tool_request_<channel_id>`` 通知。callback(channel_id,
        request_id) 於監聽執行緒被呼叫。"""
        channel = str(channel_id).strip()
        if not _CHANNEL_RE.match(channel):
            raise ValueError(f"INVALID_NOTIFY_CHANNEL:{channel_id!r}")
        with self._lock:
            subs = self._subscribers.setdefault(channel, [])
            if callback in subs:
                return
            subs.append(callback)
            new_channel = len(subs) == 1
        # 只有「新 channel」才需要重新 LISTEN；同 channel 加回呼不必重連。
        if new_channel:
            self._resubscribe.set()

    def unsubscribe(
        self, channel_id: str, callback: Callable[[str, str], None]
    ) -> None:
        removed_channel = False
        with self._lock:
            key = str(channel_id).strip()
            subs = self._subscribers.get(key)
            if subs and callback in subs:
                subs.remove(callback)
                if not subs:
                    self._subscribers.pop(key, None)
                    removed_channel = True
        if removed_channel:
            self._resubscribe.set()

    def subscribed_channels(self) -> list[str]:
        with self._lock:
            return sorted(self._subscribers)

    # -- lifecycle ------------------------------------------------------

    def start(self) -> bool:
        """啟動監聽執行緒（daemon）。無訂閱者時不開連線。"""
        if not self.subscribed_channels():
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="transport-notify-listener",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._resubscribe.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        if self._dispatch_executor is not None:
            try:
                self._dispatch_executor.shutdown(wait=False)
            except Exception:
                pass
            self._dispatch_executor = None

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "channels": self.subscribed_channels(),
            "last_error": self._last_error,
            "dispatch_stalls": self._dispatch_stalls,
        }

    # -- listener loop --------------------------------------------------

    def _manager(self) -> Any:
        factory = self._manager_factory
        if factory is not None:
            return factory()
        from .database.connection import get_connection_manager

        return get_connection_manager()

    def _run(self) -> None:
        backoff = self._min_backoff
        while not self._stop.is_set():
            channels = self.subscribed_channels()
            if not channels:
                # 無訂閱者——等待訂閱或停止（不持連線）。
                self._resubscribe.wait(timeout=1.0)
                self._resubscribe.clear()
                continue
            try:
                self._listen_once(channels)
                backoff = self._min_backoff
            except Exception as error:
                self._last_error = f"{type(error).__name__}: {error}"
                _logger.warning(
                    "transport notify listener reconnect in %.1fs: %s",
                    backoff,
                    error,
                )
                self._stop.wait(timeout=backoff)
                backoff = min(self._max_backoff, backoff * 2.0)

    def _listen_once(self, channels: list[str]) -> None:
        from psycopg import sql as _sql

        with self._manager().dedicated_connection() as conn:
            for channel in channels:
                conn.execute(
                    _sql.SQL("LISTEN {}").format(
                        _sql.Identifier(_NOTIFY_CHANNEL_PREFIX + channel)
                    )
                )
            self._resubscribe.clear()
            while not self._stop.is_set() and not self._resubscribe.is_set():
                try:
                    for notify in conn.notifies(timeout=self._poll_slice):
                        self._dispatch(notify.channel, notify.payload)
                except TimeoutError:
                    pass  # 無通知——迴圈檢查 stop/resubscribe

    _CALLBACK_DEADLINE_S = 5.0
    _DISPATCH_INFLIGHT_CAP = 8

    def _dispatch(self, pg_channel: str, payload: str) -> None:
        channel = pg_channel.removeprefix(_NOTIFY_CHANNEL_PREFIX)
        with self._lock:
            callbacks = list(self._subscribers.get(channel, ()))
        if self._dispatch_executor is None:
            self._dispatch_executor = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="transport-notify-cb"
            )
        for callback in callbacks:
            if self._dispatch_inflight >= self._DISPATCH_INFLIGHT_CAP:
                # Workers are wedged — drop the wake-hint rather than
                # grow an unbounded queue (polling fallback covers loss).
                self._dispatch_stalls += 1
                _logger.warning(
                    "notify dispatch queue saturated; dropping "
                    "(channel=%s)", channel,
                )
                continue
            self._dispatch_inflight += 1

            def _run(cb=callback, ch=channel, pl=payload) -> None:
                try:
                    cb(ch, pl)
                finally:
                    self._dispatch_inflight -= 1

            try:
                self._dispatch_executor.submit(_run).result(
                    timeout=self._CALLBACK_DEADLINE_S
                )
            except FuturesTimeoutError:
                self._dispatch_stalls += 1
                _logger.warning(
                    "notify subscriber callback exceeded %.0fs deadline "
                    "(channel=%s)",
                    self._CALLBACK_DEADLINE_S,
                    channel,
                )
            except Exception as error:
                _logger.warning("notify subscriber error: %s", error)


_LISTENER: "TransportNotifyListener | None" = None
_LISTENER_LOCK = threading.Lock()


def get_transport_notify_listener() -> TransportNotifyListener:
    """共享監聽器（每行程一條 dedicated LISTEN 連線）。"""
    global _LISTENER
    with _LISTENER_LOCK:
        if _LISTENER is None:
            _LISTENER = TransportNotifyListener()
        return _LISTENER


def peek_transport_notify_listener() -> "TransportNotifyListener | None":
    with _LISTENER_LOCK:
        return _LISTENER


__all__ = [
    "TransportNotifyListener",
    "get_transport_notify_listener",
    "peek_transport_notify_listener",
]
