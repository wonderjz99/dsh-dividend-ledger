"""离线 fixture provider：从本地 JSON 读分红事件，零网络（SPEC §5.1）。

数据文件同样接受裸数组或 ``{"events": [...]}`` 信封；坏行进 ``rejected`` 而不中断，
除非整份文件不可读。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Sequence

from dividend_ledger.errors import DataError, ProviderError
from dividend_ledger.jsonio import load_json
from dividend_ledger.models import DividendEvent
from dividend_ledger.providers.base import DividendProvider, ProviderResult
from dividend_ledger.store import RejectedItem

__all__ = ["FixtureProvider", "load_fixture_events"]


def load_fixture_events(
    path: Path,
) -> tuple[list[DividendEvent], list[RejectedItem]]:
    """从文件读取 fixture 事件。

    Raises:
        DataError: 文件缺失、不是合法 JSON，或顶层结构不对。
        ProviderError: 归一化后的记录内部不一致（如重复自然键）。
    """
    payload = load_json(path, required=True)
    if isinstance(payload, list):
        raw_records: list[Any] = list(payload)
    elif isinstance(payload, dict) and isinstance(payload.get("events"), list):
        raw_records = list(payload["events"])
    else:
        raise DataError(
            f"{path} 顶层必须是事件数组，或 {{\"events\": [...]}} 信封"
        )

    events: list[DividendEvent] = []
    rejected: list[RejectedItem] = []
    for index, raw in enumerate(raw_records):
        try:
            events.append(DividendEvent.from_dict(raw, where=f"events[{index}]"))
        except DataError as exc:
            rejected.append(
                RejectedItem(index=index, reason=str(exc), payload=raw)
            )

    seen: dict[tuple[str, str], int] = {}
    for index, event in enumerate(events):
        previous = seen.get(event.natural_key)
        if previous is not None:
            raise ProviderError(
                f"{path} 中 {event.code}@{event.ex_date.isoformat()} 出现多次"
                f"（第 {previous + 1} 条与第 {index + 1} 条）；"
                "分红事件是声明式快照，同一 (代码, 除权除息日) 只应有一条记录"
            )
        seen[event.natural_key] = index

    return events, rejected


class FixtureProvider(DividendProvider):
    """从本地文件读事件，按代码与除权除息日窗口过滤。"""

    name = "fixture"
    shares_basis = 1

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._loaded = False
        self._events: list[DividendEvent] = []
        self._rejected: list[RejectedItem] = []

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._events, self._rejected = load_fixture_events(self.path)
            for event in self._events:
                self._assert_per_share_scale(event)
            self._loaded = True

    def list_events(
        self, codes: Sequence[str], start: date, end: date
    ) -> ProviderResult:
        self._ensure_loaded()
        wanted = set(codes)
        selected = tuple(
            event
            for event in self._events
            if event.code in wanted and start <= event.ex_date <= end
        )
        return ProviderResult(
            events=selected,
            rejected=tuple(self._rejected),
            provenance={
                "provider": self.name,
                "path": str(self.path),
                "available_events": len(self._events),
                "matched_events": len(selected),
            },
        )
