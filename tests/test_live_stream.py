"""Live stream 单元测试：SSE 订阅中心、轮询/推送 feed、流服务生命周期。

全部使用注入的 fake loader / fake push connector；不启动 HTTP 服务器、
不导入 Futu SDK、不写审计。
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any, Mapping

from src.gateway import GatewayErrorCode
from src.live_data import LiveDataError
from src.live_stream import (
    LiveStreamService,
    PollingQuoteFeed,
    PushQuoteFeed,
    StreamCapacityError,
    StreamHub,
)


def _payload(price: float, *, freshness: str = "STALE") -> dict[str, Any]:
    return {
        "mode": "LIVE",
        "capturedAt": "2026-08-16T00:00:00+00:00",
        "freshness": freshness,
        "ttlSeconds": 2.0,
        "quotes": [{"code": "HK.00700", "last": price, "prevClose": 441.0}],
    }


class StreamHubTests(unittest.TestCase):
    def test_subscribe_publish_delivery(self) -> None:
        hub = StreamHub()
        sub = hub.subscribe()
        self.assertEqual(hub.publish("quote", {"a": 1}), 1)
        kind, name, payload = hub.next_event(sub, timeout=1)
        self.assertEqual((kind, name), ("event", "quote"))
        self.assertEqual(payload, {"a": 1})

    def test_timeout_without_event(self) -> None:
        hub = StreamHub()
        sub = hub.subscribe()
        kind, name, payload = hub.next_event(sub, timeout=0)
        self.assertEqual(kind, "timeout")
        self.assertIsNone(name)
        self.assertIsNone(payload)

    def test_subscriber_limit(self) -> None:
        hub = StreamHub(max_subscribers=2)
        hub.subscribe()
        hub.subscribe()
        with self.assertRaises(StreamCapacityError) as raised:
            hub.subscribe()
        self.assertEqual(raised.exception.max_subscribers, 2)

    def test_unsubscribe_closes_waiter(self) -> None:
        hub = StreamHub()
        sub = hub.subscribe()
        hub.unsubscribe(sub)
        kind, _name, _payload = hub.next_event(sub, timeout=1)
        self.assertEqual(kind, "closed")

    def test_close_is_terminal_and_wakes_waiters(self) -> None:
        hub = StreamHub()
        sub = hub.subscribe()
        hub.close()
        self.assertEqual(hub.publish("quote", {}), 0)
        kind, _name, _payload = hub.next_event(sub, timeout=1)
        self.assertEqual(kind, "closed")
        with self.assertRaises(StreamCapacityError):
            hub.subscribe()

    def test_slow_subscriber_drops_newest_events(self) -> None:
        hub = StreamHub(queue_limit=2)
        sub = hub.subscribe()
        hub.publish("quote", {"n": 1})
        hub.publish("quote", {"n": 2})
        hub.publish("quote", {"n": 3})  # dropped for the slow subscriber
        first = hub.next_event(sub, timeout=0)
        second = hub.next_event(sub, timeout=0)
        third = hub.next_event(sub, timeout=0)
        self.assertEqual(first[2], {"n": 1})
        self.assertEqual(second[2], {"n": 2})
        self.assertEqual(third[0], "timeout")
        self.assertEqual(hub.stats()["droppedTotal"], 1)
        self.assertEqual(hub.stats()["eventsTotal"], 3)


class PollingQuoteFeedTests(unittest.TestCase):
    def test_publishes_only_on_change(self) -> None:
        price = {"value": 440.0}

        def loader(codes: list[str]) -> Mapping[str, Any]:
            return _payload(price["value"])

        events: list[tuple[str, Mapping[str, Any]]] = []
        poller = PollingQuoteFeed(loader, interval=0.01)

        def on_event(name: str, payload: Mapping[str, Any]) -> None:
            events.append((name, payload))

        poller.start(["HK.00700"], on_event)
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and poller.polls < 3:
                time.sleep(0.05)
            self.assertGreaterEqual(poller.polls, 3, "poller must run several ticks")
            time.sleep(0.2)  # 同一价格再跑几轮
            self.assertEqual(len(events), 1, "unchanged quotes must be deduped")
            self.assertEqual(events[0][0], "quote")
            self.assertEqual(events[0][1]["quotes"][0]["last"], 440.0)

            price["value"] = 441.5
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if any(
                    payload.get("quotes")
                    and payload["quotes"][0]["last"] == 441.5
                    for _name, payload in events
                ):
                    break
                time.sleep(0.05)
            found = any(
                payload.get("quotes") and payload["quotes"][0]["last"] == 441.5
                for _name, payload in events
            )
            self.assertTrue(found, "price change must be published")
            quotes = [payload for name, payload in events if name == "quote"]
            self.assertEqual(len(quotes), 2, "440.0 一次 + 441.5 一次，其余去重")
        finally:
            poller.stop()

    def test_error_deduped_and_recovery_forces_quote(self) -> None:
        mode = {"fail": True}

        def loader(codes: list[str]) -> Mapping[str, Any]:
            if mode["fail"]:
                raise LiveDataError(
                    GatewayErrorCode.OPEND_UNAVAILABLE,
                    "OpenD is unavailable on the configured loopback endpoint",
                    True,
                )
            return _payload(440.0)

        events: list[tuple[str, Mapping[str, Any]]] = []
        recovered = threading.Event()
        poller = PollingQuoteFeed(loader, interval=0.01)

        def on_event(name: str, payload: Mapping[str, Any]) -> None:
            events.append((name, payload))
            if name == "quote":
                recovered.set()

        poller.start(["HK.00700"], on_event)
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and poller.polls < 5:
                time.sleep(0.05)
            self.assertGreaterEqual(poller.polls, 5, "several error ticks expected")
            errors = [p for name, p in events if name == "error"]
            self.assertEqual(len(errors), 1, "identical errors must be deduped")
            self.assertEqual(errors[0]["code"], "OPEND_UNAVAILABLE")

            mode["fail"] = False
            self.assertTrue(recovered.wait(2.0), "recovery must force a quote event")
        finally:
            poller.stop()

    def test_stop_stops_thread(self) -> None:
        poller = PollingQuoteFeed(lambda codes: _payload(440.0), interval=0.01)
        poller.start(["HK.00700"], lambda name, payload: None)
        self.assertTrue(poller.alive)
        poller.stop()
        self.assertFalse(poller.alive)


class PushQuoteFeedTests(unittest.TestCase):
    def test_connector_failure_falls_back_to_polling(self) -> None:
        warning_seen = threading.Event()
        quote_seen = threading.Event()

        def broken_connector(codes: list[str], on_quote: Any) -> Any:
            raise RuntimeError("subscribe rejected")

        def row_wrapper(codes: list[str], rows: list[Mapping[str, Any]]) -> Mapping[str, Any]:
            return {
                "mode": "LIVE",
                "capturedAt": "2026-08-16T00:00:00+00:00",
                "freshness": "FRESH",
                "ttlSeconds": None,
                "quotes": list(rows),
            }

        def on_event(name: str, payload: Mapping[str, Any]) -> None:
            if name == "warning":
                warning_seen.set()
            elif name == "quote":
                quote_seen.set()

        feed = PushQuoteFeed(
            broken_connector,
            lambda codes: _payload(440.0),
            interval=0.01,
            silence_seconds=0.05,
            row_wrapper=row_wrapper,
        )
        feed.start(["HK.00700"], on_event)
        try:
            self.assertTrue(warning_seen.wait(2.0), "fallback warning expected")
            self.assertTrue(quote_seen.wait(2.0), "polling fallback must deliver quotes")
        finally:
            feed.stop()

    def test_push_rows_become_quote_events(self) -> None:
        sink: dict[str, Any] = {}
        quote_seen = threading.Event()
        captured: dict[str, Any] = {}

        class FakeHandle:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        def fake_connector(codes: list[str], on_quote: Any) -> Any:
            sink["on_quote"] = on_quote
            captured["handle"] = FakeHandle()
            return captured["handle"]

        def row_wrapper(codes: list[str], rows: list[Mapping[str, Any]]) -> Mapping[str, Any]:
            return {
                "mode": "LIVE",
                "capturedAt": "2026-08-16T00:00:00+00:00",
                "freshness": "FRESH",
                "ttlSeconds": None,
                "quotes": [
                    {"code": "HK.00700", "last": row["last_price"]} for row in rows
                ],
            }

        def on_event(name: str, payload: Mapping[str, Any]) -> None:
            if name == "quote":
                quote_seen.set()

        feed = PushQuoteFeed(
            fake_connector,
            lambda codes: _payload(440.0),
            interval=0.05,
            silence_seconds=5.0,
            row_wrapper=row_wrapper,
        )
        feed.start(["HK.00700"], on_event)
        try:
            self.assertIn("on_quote", sink)
            sink["on_quote"]([{"code": "HK.00700", "last_price": 441.0}])
            self.assertTrue(quote_seen.wait(2.0), "pushed rows must become quote events")
            self.assertFalse(captured["handle"].closed)
        finally:
            feed.stop()
            self.assertTrue(captured["handle"].closed)

    def test_silence_watchdog_engages_fallback(self) -> None:
        sink: dict[str, Any] = {}
        warning_seen = threading.Event()

        class FakeHandle:
            def close(self) -> None:
                pass

        def fake_connector(codes: list[str], on_quote: Any) -> Any:
            sink["on_quote"] = on_quote
            return FakeHandle()

        def on_event(name: str, payload: Mapping[str, Any]) -> None:
            if name == "warning":
                warning_seen.set()

        feed = PushQuoteFeed(
            fake_connector,
            lambda codes: _payload(440.0),
            interval=0.01,
            silence_seconds=0.1,
            row_wrapper=lambda codes, rows: {},
        )
        feed.start(["HK.00700"], on_event)
        try:
            self.assertTrue(
                warning_seen.wait(2.0), "silence watchdog must engage polling fallback"
            )
        finally:
            feed.stop()


class LiveStreamServiceTests(unittest.TestCase):
    def test_subscribe_shares_feed_and_unsubscribe_stops_it(self) -> None:
        service = LiveStreamService(
            lambda codes: _payload(440.0),
            poll_interval=0.05,
            max_subscribers=8,
        )
        sub1, payload, error = service.subscribe(["HK.00700"])
        self.assertIsNotNone(payload)
        self.assertIsNone(error)
        self.assertEqual(service.hub.subscriber_count, 1)
        self.assertEqual(len(service._feeds), 1)

        sub2, _payload2, _error2 = service.subscribe(["HK.00700"])
        self.assertEqual(len(service._feeds), 1, "同一 codes 必须共享 feed")
        self.assertEqual(service.hub.subscriber_count, 2)

        kind, name, event_payload = service.next_event(sub2, timeout=0)
        self.assertEqual(kind, "timeout")

        service.unsubscribe(sub1, ["HK.00700"])
        self.assertEqual(service.hub.subscriber_count, 1)
        service.unsubscribe(sub2, ["HK.00700"])
        self.assertEqual(service.hub.subscriber_count, 0)
        self.assertEqual(len(service._feeds), 0, "最后一个订阅离开后 feed 必须停止")

    def test_subscribe_surfaces_initial_load_error(self) -> None:
        def failing_loader(codes: list[str]) -> Mapping[str, Any]:
            raise LiveDataError(
                GatewayErrorCode.OPEND_UNAVAILABLE,
                "OpenD is unavailable on the configured loopback endpoint",
                True,
            )

        service = LiveStreamService(failing_loader, poll_interval=0.05)
        sub, payload, error = service.subscribe(["HK.00700"])
        self.assertIsNone(payload)
        self.assertEqual((error or {}).get("code"), "OPEND_UNAVAILABLE")
        self.assertEqual(service.hub.subscriber_count, 1)
        service.close()
        kind, _name, _payload = service.next_event(sub, timeout=0)
        self.assertEqual(kind, "closed")

    def test_feed_events_reach_subscribers(self) -> None:
        service = LiveStreamService(
            lambda codes: _payload(440.0),
            poll_interval=0.05,
            max_subscribers=8,
        )
        sub, _payload, _error = service.subscribe(["HK.00700"])
        service.hub.publish("refresh", {"reason": "state_rebuilt"})
        kind, name, payload = service.next_event(sub, timeout=1)
        self.assertEqual(kind, "event")
        self.assertEqual(name, "refresh")
        self.assertEqual(payload, {"reason": "state_rebuilt"})
        service.unsubscribe(sub, ["HK.00700"])

    def test_close_stops_everything(self) -> None:
        service = LiveStreamService(
            lambda codes: _payload(440.0),
            poll_interval=0.05,
            max_subscribers=8,
        )
        sub, _payload, _error = service.subscribe(["HK.00700"])
        service.close()
        self.assertTrue(service.hub.closed)
        self.assertEqual(len(service._feeds), 0)
        kind, _name, _payload = service.next_event(sub, timeout=0)
        self.assertEqual(kind, "closed")


if __name__ == "__main__":
    unittest.main()
