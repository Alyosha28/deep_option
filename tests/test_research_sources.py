"""Futu 新闻/公告/研报适配器测试（不写文件、不联网）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.research_evidence import load_research_items
from src.research_sources import (
    adapt_futu_entries,
    parse_news_api_response,
    parse_news_markdown,
)


class ApiResponseParsingTests(unittest.TestCase):
    def test_api_response_maps_kinds_times_and_ids(self):
        raw = {
            "code": 0,
            "message": "ok",
            "data": [
                {
                    "news_id": 1001,
                    "news_type": 1,
                    "title": "Tencent buyback news",
                    "publish_time": 1772298000,
                    "url": "https://example.com/news/1001",
                },
                {
                    "news_id": 1002,
                    "news_type": 2,
                    "title": "Tencent notice",
                    "publish_time": 1772298000000,
                    "url": "https://example.com/notice/1002",
                },
                {
                    "news_id": 1003,
                    "news_type": 3,
                    "title": "Tencent research report",
                    "publish_time": "2026-03-30 18:12:00",
                    "url": "https://example.com/research/1003",
                },
            ],
        }

        entries = parse_news_api_response(raw, keyword="Tencent")

        self.assertEqual([entry["kind"] for entry in entries], ["news", "announcement", "research"])
        self.assertEqual(entries[0]["id"], "futu-1-1001")
        self.assertEqual(
            entries[1]["published_at"],
            datetime.fromtimestamp(1772298000).isoformat(timespec="seconds"),
        )
        self.assertEqual(entries[2]["published_at"], "2026-03-30 18:12:00")
        self.assertTrue(all(entry["url"].startswith("https://") for entry in entries))
        self.assertFalse(any(entry["synthetic"] for entry in entries))

    def test_api_error_raises(self):
        with self.assertRaisesRegex(ValueError, "error code"):
            parse_news_api_response({"code": 1, "message": "rate limited"}, keyword="Tencent")

    def test_missing_title_raises(self):
        with self.assertRaisesRegex(ValueError, "missing title"):
            parse_news_api_response(
                {"code": 0, "data": [{"news_id": 1, "news_type": 1}]},
                keyword="Tencent",
            )


class MarkdownParsingTests(unittest.TestCase):
    NEWS_SEARCH_MD = """Tencent latest news (sorted by time):

1. Tencent buybacks continued for three sessions
Publish time: 2026-03-30 18:12:00
URL: https://example.com/news/a

2. Southbound funds kept net buying
Publish time: 2026-03-30 15:48:00
URL: https://example.com/news/b

The above content is compiled from public information and does not constitute investment advice.
"""

    DIGEST_MD = """Tencent Holdings (0700.HK) stock digest

Conclusion:
Buybacks provide support.

Key evidence:

1. Tencent repurchased shares for three straight sessions
https://example.com/news/a

2. Southbound funds recorded net buying
https://example.com/news/b

This content is based on public information and does not constitute investment advice.
"""

    def test_news_search_markdown_is_parsed(self):
        entries = parse_news_markdown(self.NEWS_SEARCH_MD, symbol="Tencent")

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["title"], "Tencent buybacks continued for three sessions")
        self.assertEqual(entries[0]["published_at"], "2026-03-30 18:12:00")
        self.assertEqual(entries[0]["url"], "https://example.com/news/a")
        self.assertEqual(entries[1]["id"], "futu-md-2")

    def test_digest_markdown_marks_unknown_publish_time(self):
        entries = parse_news_markdown(self.DIGEST_MD, symbol="Tencent")

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["published_at"], "")
        self.assertEqual(entries[0]["url"], "https://example.com/news/a")
        bundle = adapt_futu_entries(entries, symbol="Tencent", synthetic=True)
        self.assertTrue(bundle["items"][0]["publish_time_unknown"])
        self.assertTrue(bundle["items"][0]["synthetic"])


class AdaptAndRoundTripTests(unittest.TestCase):
    def test_canonical_bundle_feeds_evidence_layer(self):
        raw = {
            "code": 0,
            "data": [
                {
                    "news_id": 1,
                    "news_type": 1,
                    "title": "Tencent buyback news",
                    "publish_time": 1772298000,
                    "url": "https://example.com/news/1",
                },
                {
                    "news_id": 2,
                    "news_type": 2,
                    "title": "Tencent notice",
                    "publish_time": 1772298000,
                    "url": "https://example.com/notice/2",
                },
            ],
        }
        entries = parse_news_api_response(raw, keyword="Tencent")
        bundle = adapt_futu_entries(entries, symbol="Tencent", synthetic=True)

        with tempfile.TemporaryDirectory() as temp_dir:
            out_path = Path(temp_dir, "futu_items.json")
            out_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
            items = load_research_items(out_path)

        self.assertEqual(len(items), 2)
        self.assertTrue(all(item["sha256"] for item in items))
        self.assertTrue(all(item["synthetic"] for item in items))
        self.assertEqual(items[0]["kind"], "news")
        self.assertEqual(items[1]["kind"], "announcement")

    def test_dedupe_prefers_first_entry(self):
        raw = {
            "code": 0,
            "data": [
                {
                    "news_id": 1,
                    "news_type": 1,
                    "title": "Same headline",
                    "publish_time": 1772298000,
                    "url": "https://example.com/a",
                },
                {
                    "news_id": 2,
                    "news_type": 1,
                    "title": "Same headline",
                    "publish_time": 1772298000,
                    "url": "https://example.com/a",
                },
            ],
        }
        entries = parse_news_api_response(raw, keyword="Tencent")
        bundle = adapt_futu_entries(entries, symbol="Tencent")

        self.assertEqual(len(bundle["items"]), 1)


if __name__ == "__main__":
    unittest.main()
