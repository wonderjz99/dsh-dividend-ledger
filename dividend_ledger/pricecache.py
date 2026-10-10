"""收盘价缓存。

**为什么需要**：取价走 `westock kline`，而 westock 的限频很紧，所以 runner 里
每只股票之间要间隔若干秒。3 只持仓就要 16 秒，面板每次打开都等这么久是不可接受的。

**为什么缓存是安全的**：`kline --limit 1` 取的是**上一交易日收盘价**，一天只变一次。
所以按"交易日"缓存，同一天内重复读取不会失真——这不是为了快而牺牲准确性，
而是这类数据本来就没有日内变化。

**为什么按自然日而不是交易日**：判断交易日需要交易日历（多一次请求，且又是限频）。
自然日内收盘价不会变，用自然日做键已经足够严格；跨日自然失效。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from dividend_ledger.errors import DataError
from dividend_ledger.jsonio import dump_json_atomic

__all__ = ["PriceCache", "load_price_cache"]

#: 缓存结构版本；不兼容时直接丢弃重建，比迁移省事
_CACHE_VERSION = 1


@dataclass
class PriceCache:
    """按代码 → ``{"date": ..., "price": ...}`` 的收盘价缓存。"""

    path: Path
    entries: dict[str, dict[str, str]]

    @classmethod
    def load(cls, path: Path) -> "PriceCache":
        """读取缓存。文件不存在或结构不认识时返回空缓存（**不报错**）。"""
        if not path.exists():
            return cls(path=path, entries={})
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(path=path, entries={})
        if not isinstance(payload, Mapping) or payload.get("version") != _CACHE_VERSION:
            return cls(path=path, entries={})
        raw = payload.get("prices")
        if not isinstance(raw, Mapping):
            return cls(path=path, entries={})
        entries: dict[str, dict[str, str]] = {}
        for code, item in raw.items():
            if not isinstance(item, Mapping):
                continue
            day = item.get("date")
            price = item.get("price")
            if isinstance(day, str) and isinstance(price, str):
                entries[str(code)] = {"date": day, "price": price}
        return cls(path=path, entries=entries)

    def get(self, code: str, *, today: date) -> Decimal | None:
        """取某只股票**今天**有效的缓存价格；过期或损坏时返回 ``None``。"""
        item = self.entries.get(code)
        if item is None or item.get("date") != today.isoformat():
            return None
        try:
            value = Decimal(item["price"])
        except (InvalidOperation, KeyError):
            return None
        return value if value > 0 else None

    def put(self, code: str, price: Decimal, *, today: date) -> None:
        self.entries[code] = {"date": today.isoformat(), "price": str(price)}

    def drop_stale(self, *, today: date) -> None:
        """清掉非今天的条目，避免文件无限增长。"""
        stamp = today.isoformat()
        self.entries = {
            code: item for code, item in self.entries.items() if item.get("date") == stamp
        }

    def save(self) -> None:
        """写回缓存。

        Raises:
            DataError: 写入失败。调用方通常应吞掉——缓存写不进去不该让功能不可用。
        """
        try:
            dump_json_atomic(
                self.path,
                {"version": _CACHE_VERSION, "prices": dict(sorted(self.entries.items()))},
            )
        except OSError as exc:
            raise DataError(f"写价格缓存失败：{exc}") from None


def load_price_cache(data_dir: Path) -> PriceCache:
    """约定的缓存位置：``<data>/cache/prices.json``。"""
    return PriceCache.load(data_dir / "cache" / "prices.json")


def to_json(payload: Any) -> str:  # pragma: no cover - 仅调试用
    return json.dumps(payload, ensure_ascii=False)
