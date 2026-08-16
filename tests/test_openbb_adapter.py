"""可选 OpenBB 行情适配器测试：不要求安装 OpenBB 或访问外网。"""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.openbb_adapter import add_realized_volatility, fetch_historical, normalize_symbol


class _FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):
        if orient != "records":
            raise AssertionError("adapter should request record rows")
        return list(self._rows)


class _FakeIndexedFrame(_FakeFrame):
    def __init__(self, rows, index):
        super().__init__(rows)
        self.index = index


class _FakeResult:
    provider = "yfinance"
    warnings = None

    def __init__(self, rows):
        self.rows = rows

    def to_dataframe(self):
        return _FakeFrame(self.rows)


class _FakeIndexedResult(_FakeResult):
    def to_dataframe(self):
        return _FakeIndexedFrame(self.rows, ["2026-08-07"])


class _FakeHistorical:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResult(self.rows)


class _FakeIndexedHistorical(_FakeHistorical):
    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeIndexedResult(self.rows)


class OpenBBAdapterTests(unittest.TestCase):
    def test_adds_annualized_realized_volatility_after_minimum_history(self):
        points = [
            {"date": f"2026-08-{day:02d}", "close": close}
            for day, close in enumerate([100, 101, 99, 102, 101, 103, 104], start=1)
        ]

        enriched, metrics = add_realized_volatility(points, window=5)

        self.assertIsNone(enriched[4]["hv5d"])
        self.assertIsNotNone(enriched[5]["hv5d"])
        self.assertIsNotNone(enriched[-1]["hv5d"])
        self.assertEqual(metrics["volatilityRows"], 2)
        self.assertEqual(metrics["lastDate"], "2026-08-07")
        self.assertAlmostEqual(metrics["latestHv30d"], enriched[-1]["hv5d"])

    def test_normalizes_hong_kong_symbol_for_yfinance(self):
        self.assertEqual(normalize_symbol("HK.00700", "yfinance"), "0700.HK")
        self.assertEqual(normalize_symbol("0700.HK", "yfinance"), "0700.HK")
        self.assertEqual(normalize_symbol("HK.00700", "fmp"), "HK.00700")

    def test_fetches_and_serializes_historical_rows(self):
        historical = _FakeHistorical(
            [
                {
                    "date": "2026-08-07",
                    "open": 470,
                    "high": 482,
                    "low": 468,
                    "close": 479,
                    "volume": 1200,
                }
            ]
        )
        fake_obb = SimpleNamespace(
            equity=SimpleNamespace(price=SimpleNamespace(historical=historical))
        )

        result = fetch_historical(
            "HK.00700",
            provider="yfinance",
            start_date="2026-08-01",
            end_date="2026-08-08",
            obb_module=fake_obb,
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["symbol"], "0700.HK")
        self.assertEqual(result["provider"], "yfinance")
        self.assertEqual(result["points"][0]["close"], 479.0)
        self.assertEqual(historical.calls[0]["interval"], "1d")
        self.assertEqual(historical.calls[0]["start_date"], "2026-08-01")

    def test_missing_openbb_is_a_nonfatal_provider_status(self):
        result = fetch_historical("HK.00700", obb_module=False)

        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "not_installed")
        self.assertEqual(result["points"], [])

    def test_yfinance_direct_fallback_serializes_daily_history(self):
        class Frame:
            def reset_index(self):
                return self

            def to_dict(self, orient):
                self.assert_orient = orient
                return [
                    {
                        "Date": "2026-08-07",
                        "Open": 470,
                        "High": 482,
                        "Low": 468,
                        "Close": 479,
                        "Volume": 1200,
                    }
                ]

        class Ticker:
            def __init__(self, symbol):
                self.symbol = symbol
                self.calls = []

            def history(self, **kwargs):
                self.calls.append(kwargs)
                return Frame()

        ticker = Ticker("0700.HK")
        fake_yfinance = SimpleNamespace(Ticker=lambda symbol: ticker)
        with patch("src.openbb_adapter._load_openbb", side_effect=ModuleNotFoundError):
            with patch.dict("sys.modules", {"yfinance": fake_yfinance}):
                result = fetch_historical("HK.00700", provider="yfinance", period="1y")

        self.assertTrue(result["available"])
        self.assertEqual(result["source"], "yfinance direct")
        self.assertEqual(result["points"][0]["close"], 479.0)
        self.assertEqual(ticker.calls[0]["period"], "1y")

    def test_empty_provider_result_is_a_nonfatal_provider_status(self):
        historical = _FakeHistorical([])
        fake_obb = SimpleNamespace(
            equity=SimpleNamespace(price=SimpleNamespace(historical=historical))
        )

        result = fetch_historical("HK.00700", obb_module=fake_obb)

        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "no_rows")
        self.assertEqual(result["points"], [])

    def test_uses_dataframe_index_when_openbb_omits_date_column(self):
        historical = _FakeIndexedHistorical(
            [{"open": 470, "high": 482, "low": 468, "close": 479, "volume": 1200}]
        )
        fake_obb = SimpleNamespace(
            equity=SimpleNamespace(price=SimpleNamespace(historical=historical))
        )

        result = fetch_historical("HK.00700", obb_module=fake_obb)

        self.assertTrue(result["available"])
        self.assertEqual(result["points"][0]["date"], "2026-08-07")


if __name__ == "__main__":
    unittest.main()
