"""分红事件来源的抽象（SPEC §5.1）。

**分工铁律**：provider 只负责"把外部数据翻译成规范字段"，**不参与任何业务计算**。
归属匹配、金额乘法、汇总全部在 :mod:`dividend_ledger.ledger` 与
:mod:`dividend_ledger.report` 里做——这样换数据源永远不会影响账本口径。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence

from dividend_ledger.errors import ProviderError
from dividend_ledger.models import DividendEvent
from dividend_ledger.store import RejectedItem

__all__ = ["DividendProvider", "ProviderResult"]


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """一次取数的结果：事件 + 被拒绝的行 + 溯源信息。"""

    events: tuple[DividendEvent, ...]
    rejected: tuple[RejectedItem, ...] = ()
    #: 供报告 ``meta`` 记录的来源信息（provider 名、命令、抓取时间等）
    provenance: dict[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "rejected", tuple(self.rejected))


class DividendProvider(ABC):
    """分红事件来源。

    实现者必须保证：

    1. ``dividend_per_share`` 是**每股**税前金额。源数据是"每 10 股"口径时必须
       ÷10——:meth:`_assert_per_share_scale` 会做一次量纲体检，把差 10 倍的错误
       挡在账本之外。
    2. 不做归属过滤（不要自己判断"用户在不在持有期"），只按代码与日期窗口取数。
    3. 不吞掉坏数据：无法解析的行要放进 :attr:`ProviderResult.rejected`。
    """

    #: provider 标识，写入事件的 ``source`` 字段
    name: str = "unknown"

    #: 源数据的"每 N 股"口径；westock 为 10，其余一般为 1
    shares_basis: int = 1

    @abstractmethod
    def list_events(
        self, codes: Sequence[str], start: date, end: date
    ) -> ProviderResult:
        """取指定代码在 ``[start, end]`` 区间内的分红事件。

        Args:
            codes: 归一化后的证券代码列表。
            start: 区间起（含）。
            end: 区间止（含）。

        Returns:
            :class:`ProviderResult`。
        """

    def _assert_per_share_scale(self, event: DividendEvent) -> None:
        """量纲体检：每股派息/送转比例不应大到离谱。

        A 股每股派息在 0–10 元量级、送转比例在 0–1 量级（10 送 10）。
        若 provider 忘了把"每 10 股"换算成"每股"，数值会正好大 10 倍。
        这里用宽阈值兜底报警，而不是静默接受——因为一旦进账本就是 10 倍误差。
        """
        if event.dividend_per_share > Decimal("100"):
            raise ProviderError(
                f"{self.name}: {event.code}@{event.ex_date.isoformat()} 的每股派息为 "
                f"{event.dividend_per_share}，远超 A 股常见量级。"
                f"源数据是否为「每 {self.shares_basis} 股」口径却未换算？"
            )
        for label, value in (
            ("per_share_bonus", event.per_share_bonus),
            ("per_share_transfer", event.per_share_transfer),
        ):
            if value > Decimal("10"):
                raise ProviderError(
                    f"{self.name}: {event.code}@{event.ex_date.isoformat()} 的 {label} 为 "
                    f"{value}，远超 A 股常见量级（≤1 表示 10 送/转 10）。"
                    f"源数据是否为「每 {self.shares_basis} 股」口径却未换算？"
                )
