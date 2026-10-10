"""收盘价缓存测试。

缓存存在的理由是 WESTOCK 限频很紧：3 只持仓取价要 17 秒，面板每次打开都等这么
久不可接受。而 ``kline --limit 1`` 取的是**上一交易日收盘价**，一天只变一次，
所以按自然日缓存不损失准确性。

测试盯三件事：按日失效、损坏文件不炸、过期条目被清掉。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from dividend_ledger.pricecache import PriceCache, load_price_cache

TODAY = date(2026, 10, 10)
YESTERDAY = TODAY - timedelta(days=1)


class TempCacheMixin:
    def setUp(self) -> None:  # noqa: D102
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "cache" / "prices.json"

    def tearDown(self) -> None:  # noqa: D102
        self._tmp.cleanup()


class RoundTripTest(TempCacheMixin, unittest.TestCase):
    def test_put_save_load(self) -> None:
        cache = PriceCache.load(self.path)
        cache.put("600519.SH", Decimal("1263"), today=TODAY)
        cache.save()

        reloaded = PriceCache.load(self.path)
        self.assertEqual(reloaded.get("600519.SH", today=TODAY), Decimal("1263"))

    def test_miss_returns_none(self) -> None:
        cache = PriceCache.load(self.path)
        self.assertIsNone(cache.get("600519.SH", today=TODAY))

    def test_entry_from_yesterday_is_stale(self) -> None:
        """跨日必须失效——否则第二天还显示昨天的价。"""
        cache = PriceCache.load(self.path)
        cache.put("600519.SH", Decimal("1263"), today=YESTERDAY)
        cache.save()

        reloaded = PriceCache.load(self.path)
        self.assertIsNone(reloaded.get("600519.SH", today=TODAY))

    def test_drop_stale_clears_old_entries(self) -> None:
        cache = PriceCache.load(self.path)
        cache.put("600519.SH", Decimal("1263"), today=TODAY)
        cache.put("000001.SZ", Decimal("11.59"), today=YESTERDAY)
        cache.drop_stale(today=TODAY)
        self.assertIn("600519.SH", cache.entries)
        self.assertNotIn("000001.SZ", cache.entries)

    def test_save_is_sorted_and_stable(self) -> None:
        cache = PriceCache.load(self.path)
        cache.put("600519.SH", Decimal("1263"), today=TODAY)
        cache.put("000001.SZ", Decimal("11.59"), today=TODAY)
        cache.save()
        first = self.path.read_bytes()
        cache.save()
        self.assertEqual(self.path.read_bytes(), first)


class RobustnessTest(TempCacheMixin, unittest.TestCase):
    def test_missing_file_is_empty_cache(self) -> None:
        cache = PriceCache.load(self.path)
        self.assertEqual(cache.entries, {})

    def test_corrupt_json_gives_empty_cache_not_error(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(PriceCache.load(self.path).entries, {})

    def test_unknown_version_gives_empty_cache(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"version": 99, "prices": {"x": {}}}), encoding="utf-8")
        self.assertEqual(PriceCache.load(self.path).entries, {})

    def test_bad_entry_shapes_are_skipped(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "prices": {
                        "good": {"date": TODAY.isoformat(), "price": "10"},
                        "noDate": {"price": "10"},
                        "noPrice": {"date": TODAY.isoformat()},
                        "notObject": "x",
                    },
                }
            ),
            encoding="utf-8",
        )
        cache = PriceCache.load(self.path)
        self.assertEqual(set(cache.entries), {"good"})

    def test_unparseable_price_treated_as_miss(self) -> None:
        cache = PriceCache(self.path, {"600519.SH": {"date": TODAY.isoformat(), "price": "abc"}})
        self.assertIsNone(cache.get("600519.SH", today=TODAY))

    def test_zero_price_treated_as_miss(self) -> None:
        """0 不能当价格用，否则股息率会变成无穷大。"""
        cache = PriceCache(self.path, {"600519.SH": {"date": TODAY.isoformat(), "price": "0"}})
        self.assertIsNone(cache.get("600519.SH", today=TODAY))

    def test_load_price_cache_path_convention(self) -> None:
        cache = load_price_cache(Path("/tmp/data"))
        self.assertEqual(cache.path, Path("/tmp/data/cache/prices.json"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
