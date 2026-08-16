"""Server-push live quote streaming layer (SSE).

第二阶段实时链路：把「请求时刷新 + TTL 缓存」升级为服务端主动推送。

- :class:`StreamHub`：线程安全订阅中心——每客户端有界队列、发布扇出、
  带超时等待（供 SSE handler 做心跳）、终止信号 close()。
- :class:`PollingQuoteFeed`：diff 式轮询源——以固定间隔读
  ``LiveDataService.quote_payload``，仅在报价或新鲜度变化时发布 ``quote``
  事件；typed live 失败发布 ``error`` 事件（去重，恢复后强制补一发）。
- :class:`PushQuoteFeed`：可选真实 OpenD 推送源（SDK subscribe + QuoteHandler，
  经 gateway 的可选 ``start_quote_push`` 注入）。推送失败或静默超时自动回退
  轮询源并发布一次 ``warning`` 事件。
- :class:`LiveStreamService`：hub + 按 codes 分组的 feed 生命周期
  （订阅引用计数归零即停 feed）。

铁律不变：本模块只读，不写审计、不产数字（数字全部来自 gateway/引擎）；
模块顶部不导入 Futu SDK（导入安全，测试可注入 fake）。
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from .live_data import LiveDataError, utc_now_iso

DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_HEARTBEAT_SECONDS = 15.0
DEFAULT_MAX_SUBSCRIBERS = 8
DEFAULT_QUEUE_LIMIT = 256
DEFAULT_PUSH_SILENCE_SECONDS = 60.0

_UNSET = object()


class StreamCapacityError(RuntimeError):
    """Raised when the hub refuses a new subscriber (hard limit)."""

    def __init__(self, max_subscribers: int):
        super().__init__(f"live stream subscriber limit reached ({max_subscribers})")
        self.max_subscribers = max_subscribers


class StreamHub:
    """Thread-safe SSE subscriber registry with bounded per-client queues.

    ``publish`` fans one event out to every subscriber; slow subscribers drop
    the newest event (never block the publisher) and the drop is counted.
    ``next_event`` blocks up to ``timeout`` seconds and returns
    ``("event", name, payload)`` / ``("timeout", None, None)`` /
    ``("closed", None, None)``.  After ``close`` every waiter unblocks with
    ``closed`` and the hub is terminal.
    """

    def __init__(
        self,
        *,
        max_subscribers: int = DEFAULT_MAX_SUBSCRIBERS,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if isinstance(max_subscribers, bool) or not isinstance(max_subscribers, int) or max_subscribers < 1:
            raise ValueError("max_subscribers must be a positive integer")
        if isinstance(queue_limit, bool) or not isinstance(queue_limit, int) or queue_limit < 1:
            raise ValueError("queue_limit must be a positive integer")
        self.max_subscribers = max_subscribers
        self.queue_limit = queue_limit
        self._clock = clock or time.monotonic
        self._cond = threading.Condition()
        self._queues: dict[int, deque[tuple[str, Mapping[str, Any]]]] = {}
        self._dropped: dict[int, int] = {}
        self._events_total = 0
        self._closed = False
        self._next_id = 1

    @property
    def subscriber_count(self) -> int:
        with self._cond:
            return len(self._queues)

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    def subscribe(self) -> int:
        with self._cond:
            if self._closed:
                raise StreamCapacityError(0)
            if len(self._queues) >= self.max_subscribers:
                raise StreamCapacityError(self.max_subscribers)
            sub_id = self._next_id
            self._next_id += 1
            self._queues[sub_id] = deque()
            self._dropped[sub_id] = 0
            return sub_id

    def unsubscribe(self, sub_id: int) -> None:
        with self._cond:
            self._queues.pop(sub_id, None)

    def publish(self, event: str, payload: Mapping[str, Any] | None = None) -> int:
        """Fan out one event; returns the number of subscribers it reached."""
        if not isinstance(event, str) or not event.strip():
            raise ValueError("event name must be a non-empty string")
        data = dict(payload or {})
        delivered = 0
        with self._cond:
            if self._closed:
                return 0
            for sub_id, queue in self._queues.items():
                if len(queue) >= self.queue_limit:
                    self._dropped[sub_id] = self._dropped.get(sub_id, 0) + 1
                    continue
                queue.append((event, data))
                delivered += 1
            self._events_total += 1
            self._cond.notify_all()
        return delivered

    def next_event(
        self,
        sub_id: int,
        timeout: Optional[float] = None,
    ) -> tuple[str, Optional[str], Optional[Mapping[str, Any]]]:
        """Block for the next event of one subscriber (see class docstring)."""
        deadline = None
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
                raise ValueError("timeout must be a non-negative number of seconds")
            deadline = self._clock() + float(timeout)
        with self._cond:
            while True:
                if self._closed:
                    return ("closed", None, None)
                queue = self._queues.get(sub_id)
                if queue is None:
                    return ("closed", None, None)
                if queue:
                    name, payload = queue.popleft()
                    return ("event", name, payload)
                if deadline is None:
                    self._cond.wait()
                    continue
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return ("timeout", None, None)
                self._cond.wait(remaining)

    def stats(self) -> dict[str, Any]:
        with self._cond:
            return {
                "subscribers": len(self._queues),
                "eventsTotal": self._events_total,
                "droppedTotal": sum(self._dropped.values()),
                "closed": self._closed,
            }

    def close(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._queues.clear()
            self._cond.notify_all()


class QuoteFeed:
    """Minimal feed contract: start(codes, on_event) / stop() / alive."""

    def start(self, codes: list[str], on_event: Callable[[str, Mapping[str, Any]], None]) -> None:
        raise NotImplementedError

    def stop(self, timeout: float = 5.0) -> None:
        raise NotImplementedError

    @property
    def alive(self) -> bool:
        raise NotImplementedError


class PollingQuoteFeed(QuoteFeed):
    """Diff-based poller over a ``quote_payload``-style loader.

    Publishes ``quote`` only when the quote list or freshness actually changes
    (dedup by canonical JSON fingerprint) and ``error`` on typed live failures
    (deduped per error signature; a successful poll after errors forces one
    fresh ``quote`` so clients re-sync).
    """

    def __init__(
        self,
        loader: Callable[[list[str]], Mapping[str, Any]],
        *,
        interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(float(interval))
            or float(interval) <= 0
        ):
            raise ValueError("interval must be a positive finite number of seconds")
        self._loader = loader
        self._interval = float(interval)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._codes: list[str] = []
        self._on_event: Optional[Callable[[str, Mapping[str, Any]], None]] = None
        self._last_fingerprint: Any = _UNSET
        self._last_error: Any = _UNSET
        self._polls = 0

    @property
    def alive(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def polls(self) -> int:
        with self._lock:
            return self._polls

    def start(self, codes: list[str], on_event: Callable[[str, Mapping[str, Any]], None]) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("polling feed is already running")
            self._codes = list(codes)
            self._on_event = on_event
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="goai-live-quote-poller",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            thread = self._thread
        if thread is None:
            return
        self._stop_event.set()
        thread.join(timeout)

    def _emit(self, event: str, payload: Mapping[str, Any]) -> None:
        handler = self._on_event
        if handler is None:
            return
        try:
            handler(event, dict(payload))
        except Exception:
            # A broken downstream handler must never kill the feed thread.
            pass

    def _run(self) -> None:
        while True:
            if self._stop_event.wait(self._interval):
                return
            with self._lock:
                codes = list(self._codes)
                self._polls += 1
            try:
                payload = self._loader(codes)
            except LiveDataError as exc:
                error = exc.to_dict()
                if error != self._last_error:
                    self._last_error = error
                    self._emit("error", error)
                continue
            except Exception as exc:
                error = {
                    "code": "INTERNAL_ERROR",
                    "message": f"live stream poll failed: {exc}",
                    "retryable": True,
                }
                if error != self._last_error:
                    self._last_error = error
                    self._emit("error", error)
                continue

            quotes = payload.get("quotes") if isinstance(payload, Mapping) else None
            freshness = payload.get("freshness") if isinstance(payload, Mapping) else None
            try:
                fingerprint = (
                    freshness,
                    json.dumps(quotes, ensure_ascii=False, sort_keys=True),
                )
            except (TypeError, ValueError):
                fingerprint = (freshness, repr(quotes))

            had_error = self._last_error is not _UNSET
            self._last_error = _UNSET
            if fingerprint != self._last_fingerprint or had_error:
                self._last_fingerprint = fingerprint
                self._emit("quote", payload)


class PushQuoteFeed(QuoteFeed):
    """Optional real OpenD push feed with automatic polling fallback.

    The push connector is injected (in production it is
    ``FutuLiveGateway.start_quote_push``); it receives codes and an ``on_quote``
    callback delivering whitelisted rows and returns a closeable handle.  A
    watchdog switches the feed to a :class:`PollingQuoteFeed` once after either
    a push failure or ``silence_seconds`` without any pushed tick, publishing a
    single ``warning`` event for the UI.
    """

    def __init__(
        self,
        push_connector: Optional[Callable[..., Any]],
        poll_loader: Callable[[list[str]], Mapping[str, Any]],
        *,
        interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        silence_seconds: float = DEFAULT_PUSH_SILENCE_SECONDS,
        clock: Optional[Callable[[], float]] = None,
        utc_clock: Optional[Callable[[], str]] = None,
        row_wrapper: Optional[Callable[[list[str], list[Mapping[str, Any]]], Mapping[str, Any]]] = None,
    ) -> None:
        if (
            isinstance(silence_seconds, bool)
            or not isinstance(silence_seconds, (int, float))
            or not math.isfinite(float(silence_seconds))
            or float(silence_seconds) <= 0
        ):
            raise ValueError("silence_seconds must be a positive finite number of seconds")
        self._push_connector = push_connector
        self._poll_loader = poll_loader
        self._interval = float(interval)
        self._silence_seconds = float(silence_seconds)
        self._clock = clock or time.monotonic
        self._utc_clock = utc_clock or utc_now_iso
        self._row_wrapper = row_wrapper
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._handle: Any = None
        self._watchdog: Optional[threading.Thread] = None
        self._fallback: Optional[PollingQuoteFeed] = None
        self._last_push_at: Optional[float] = None
        self._codes: list[str] = []
        self._on_event: Optional[Callable[[str, Mapping[str, Any]], None]] = None
        self._fallback_engaged = False

    @property
    def alive(self) -> bool:
        with self._lock:
            if self._fallback_engaged:
                return bool(self._fallback and self._fallback.alive)
            return self._watchdog is not None and self._watchdog.is_alive()

    def start(self, codes: list[str], on_event: Callable[[str, Mapping[str, Any]], None]) -> None:
        with self._lock:
            self._codes = list(codes)
            self._on_event = on_event
            self._stop_event.clear()
            self._last_push_at = self._clock()
            connector = self._push_connector
        # 连接器调用期间不持有 self._lock：SDK 的 is_first_push=True 会让
        # 回调在 subscribe 返回前触发（_on_push_rows 需要同一把锁），
        # 失败路径的 _engage_fallback 同样需要这把锁。
        if connector is not None:
            try:
                handle = connector(codes, self._on_push_rows)
            except Exception as exc:
                self._engage_fallback(f"push subscribe failed: {exc}")
                return
            with self._lock:
                if self._fallback_engaged:
                    try:
                        handle.close()
                    except Exception:
                        pass
                    return
                self._handle = handle
        else:
            self._engage_fallback("no push connector configured")
            return
        with self._lock:
            if self._fallback_engaged:
                return
        self._watchdog = threading.Thread(
            target=self._watch,
            name="goai-live-push-watchdog",
            daemon=True,
        )
        self._watchdog.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        with self._lock:
            handle = self._handle
            fallback = self._fallback
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        if fallback is not None:
            fallback.stop(timeout)
        with self._lock:
            thread = self._watchdog
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _on_push_rows(self, rows: list[Mapping[str, Any]]) -> None:
        with self._lock:
            self._last_push_at = self._clock()
            codes = list(self._codes)
            wrapper = self._row_wrapper
        handler = self._on_event
        if handler is None or wrapper is None:
            return
        try:
            payload = wrapper(codes, list(rows))
        except Exception:
            return
        self._emit("quote", payload)

    def _engage_fallback(self, reason: str) -> None:
        with self._lock:
            if self._fallback_engaged:
                return
            self._fallback_engaged = True
            handle = self._handle
            self._handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        self._emit("warning", {"code": "FEED_FALLBACK_POLL", "message": reason})
        fallback = PollingQuoteFeed(
            self._poll_loader,
            interval=self._interval,
            clock=self._clock,
        )
        with self._lock:
            self._fallback = fallback
            codes = list(self._codes)
            handler = self._on_event
        fallback.start(codes, self._emit)

    def _watch(self) -> None:
        while not self._stop_event.wait(min(self._silence_seconds, 5.0)):
            with self._lock:
                if self._fallback_engaged:
                    return
                last = self._last_push_at
            if self._clock() - last > self._silence_seconds:
                self._engage_fallback(
                    f"no push tick within {self._silence_seconds:g}s; switched to polling"
                )
                return

    def _emit(self, event: str, payload: Mapping[str, Any]) -> None:
        handler = self._on_event
        if handler is None:
            return
        try:
            handler(event, dict(payload))
        except Exception:
            pass


@dataclass
class _FeedEntry:
    refs: int = 0
    feed: QuoteFeed | None = None


class LiveStreamService:
    """Subscriber lifecycle + per-codes feed management for one live stream.

    ``subscribe`` starts (or refcounts) the feed for a code set and returns
    ``(sub_id, initial_payload, initial_error)``; the HTTP layer writes the
    ``hello`` event itself.  ``unsubscribe`` stops the feed when its last
    subscriber leaves.  ``close`` is terminal.
    """

    def __init__(
        self,
        quote_loader: Callable[[list[str]], Mapping[str, Any]],
        *,
        hub: Optional[StreamHub] = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        max_subscribers: int = DEFAULT_MAX_SUBSCRIBERS,
        push_connector: Optional[Callable[..., Any]] = None,
        push_silence_seconds: float = DEFAULT_PUSH_SILENCE_SECONDS,
        row_wrapper: Optional[Callable[[list[str], list[Mapping[str, Any]]], Mapping[str, Any]]] = None,
        clock: Optional[Callable[[], float]] = None,
        utc_clock: Optional[Callable[[], str]] = None,
    ) -> None:
        self.hub = hub or StreamHub(max_subscribers=max_subscribers)
        self._quote_loader = quote_loader
        self._poll_interval = float(poll_interval)
        self._push_connector = push_connector
        self._push_silence_seconds = float(push_silence_seconds)
        self._row_wrapper = row_wrapper
        self._clock = clock or time.monotonic
        self._utc_clock = utc_clock or utc_now_iso
        self._lock = threading.Lock()
        self._feeds: dict[tuple[str, ...], _FeedEntry] = {}

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    def subscribe(self, codes: list[str]) -> tuple[int, Optional[Mapping[str, Any]], Optional[Mapping[str, Any]]]:
        key = tuple(codes)
        sub_id = self.hub.subscribe()
        with self._lock:
            entry = self._feeds.get(key)
            created = entry is None
            if entry is None:
                entry = _FeedEntry(refs=0, feed=self._make_feed(key))
                self._feeds[key] = entry
            entry.refs += 1
            feed = entry.feed
        assert feed is not None
        if created:
            try:
                feed.start(list(key), self._on_feed_event)
            except Exception as exc:
                self.unsubscribe(sub_id, codes)
                raise
        payload: Optional[Mapping[str, Any]] = None
        error: Optional[Mapping[str, Any]] = None
        try:
            payload = self._quote_loader(list(key))
        except LiveDataError as exc:
            error = exc.to_dict()
        except Exception as exc:
            error = {
                "code": "INTERNAL_ERROR",
                "message": f"live stream initial load failed: {exc}",
                "retryable": True,
            }
        return sub_id, payload, error

    def unsubscribe(self, sub_id: int, codes: list[str]) -> None:
        key = tuple(codes)
        self.hub.unsubscribe(sub_id)
        with self._lock:
            entry = self._feeds.get(key)
            if entry is None:
                return
            entry.refs = max(0, entry.refs - 1)
            if entry.refs == 0:
                feed = entry.feed
                self._feeds.pop(key, None)
                if feed is not None:
                    feed.stop()

    def _make_feed(self, key: tuple[str, ...]) -> QuoteFeed:
        if self._push_connector is None:
            return PollingQuoteFeed(
                self._quote_loader,
                interval=self._poll_interval,
                clock=self._clock,
            )
        return PushQuoteFeed(
            self._push_connector,
            self._quote_loader,
            interval=self._poll_interval,
            silence_seconds=self._push_silence_seconds,
            clock=self._clock,
            utc_clock=self._utc_clock,
            row_wrapper=self._row_wrapper,
        )

    def _on_feed_event(self, event: str, payload: Mapping[str, Any]) -> None:
        self.hub.publish(event, payload)

    def next_event(self, sub_id: int, timeout: Optional[float] = None) -> tuple[str, Optional[str], Optional[Mapping[str, Any]]]:
        return self.hub.next_event(sub_id, timeout)

    def close(self) -> None:
        with self._lock:
            feeds = [entry.feed for entry in self._feeds.values() if entry.feed is not None]
            self._feeds.clear()
        for feed in feeds:
            try:
                feed.stop()
            except Exception:
                pass
        self.hub.close()


__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "DEFAULT_MAX_SUBSCRIBERS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_PUSH_SILENCE_SECONDS",
    "LiveStreamService",
    "PollingQuoteFeed",
    "PushQuoteFeed",
    "QuoteFeed",
    "StreamCapacityError",
    "StreamHub",
]
