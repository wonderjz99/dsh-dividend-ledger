"""归属匹配与单事件计算（SPEC §4.2 / §3.3 / §4.4）。

归属规则（可解释、可复核，不做任何按比例分配）::

   事件 E 归到持仓 H  ⇔  E.code == H.code 且 E.ex_date >= H.start_date

即"除权除息日在你开始持有之前的事件不属于你"——哪怕它的派息到账日落在你持有期内。

计算规则::

   现金分红   = 每股派息 × 股数          （税前，主口径）
   送股股数   = 每股送股比例 × 股数
   转增股数   = 每股转增比例 × 股数
   成本收益率 = 现金分红 ÷ (股数 × 每股成本)     缺成本 → None
   股息率     = 每股派息 ÷ 除权除息日前收盘价     缺价格 → None

**缺失一律为 None，绝不用 0 冒充。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from dividend_ledger.decimals import ZERO, format_money
from dividend_ledger.models import DateBasis, DividendEvent, Holding, LedgerEntry

__all__ = [
    "MISSING_CLOSE_PREV",
    "MISSING_COST_PER_SHARE",
    "ExcludedEvent",
    "Ledger",
    "MissingInput",
    "build_ledger",
    "build_ledger_entry",
    "filter_events_for_holding",
]

#: 缺失输入的机器可读标识
MISSING_COST_PER_SHARE = "cost_per_share"
MISSING_CLOSE_PREV = "close_prev"

_IMPACT_BY_KIND = {
    MISSING_COST_PER_SHARE: "yield_on_cost",
    MISSING_CLOSE_PREV: "dividend_yield",
}

_DESCRIPTION_BY_KIND = {
    MISSING_COST_PER_SHARE: "未提供每股成本，成本收益率无法计算",
    MISSING_CLOSE_PREV: "未提供除权除息日前收盘价，股息率无法计算",
}


@dataclass(frozen=True, slots=True)
class MissingInput:
    """一条"因为缺输入所以没算出来"的记录（SPEC §4.4）。"""

    code: str
    kind: str
    detail: str
    affected_field: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "input": self.kind,
            "affected_field": self.affected_field,
            "detail": self.detail,
        }

    def label(self) -> str:
        """形如 ``600519.SH.cost_per_share``，可直接展示给用户。"""
        return f"{self.code}.{self.kind}"


@dataclass(frozen=True, slots=True)
class ExcludedEvent:
    """一条因为归属规则被排除的事件，保留以便复核。"""

    code: str
    ex_date: date
    start_date: date
    reason: str
    dividend_per_share: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "ex_date": self.ex_date.isoformat(),
            "holding_start_date": self.start_date.isoformat(),
            "reason": self.reason,
            "dividend_per_share": str(self.dividend_per_share),
        }


@dataclass(frozen=True, slots=True)
class Ledger:
    """完整明细账。"""

    entries: tuple[LedgerEntry, ...]
    excluded: tuple[ExcludedEvent, ...]
    missing_inputs: tuple[MissingInput, ...]
    holdings_used: tuple[Holding, ...]
    events_considered: int
    date_basis_counts: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self, *, meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """序列化为 ledger.json 的内容。"""
        payload: dict[str, Any] = {
            "format_version": 1,
            "meta": dict(meta or {}),
            "entries": [entry.to_dict() for entry in self.entries],
            "excluded_events": [item.to_dict() for item in self.excluded],
            "missing_inputs": [item.to_dict() for item in self.missing_inputs],
            "stats": {
                "holdings": len(self.holdings_used),
                "entries": len(self.entries),
                "events_considered": self.events_considered,
                "excluded_events": len(self.excluded),
                "date_basis_counts": dict(sorted(self.date_basis_counts.items())),
            },
        }
        return payload


def filter_events_for_holding(
    holding: Holding, events: Iterable[DividendEvent]
) -> tuple[list[DividendEvent], list[ExcludedEvent]]:
    """把事件按归属规则分给一条持仓。

    两条归属规则，**缺一不可**：

    1. **日期**：除权除息日不早于开始持有日。
    2. **状态**：只有 ``IMPLEMENTED`` 才进明细账。``confirmed`` / ``proposed``
       是"待收"（阶段三），它们属于 :mod:`dividend_ledger.upcoming`，
       **绝不能进历史累计**。

    第 2 条曾经漏掉过——只比日期不看状态，于是未实施方案被当成已到账计入
    `totals.cash_amount`，历史合计凭空变大（实测 26856.40 被算成 33047.50）。
    这是阶段三最核心的一条不变量，由 `tests/test_upcoming.py` 守护。

    Returns:
        ``(归属事件, 被排除事件)``。两者都按 ``ex_date`` 升序，保证输出稳定。
    """
    included: list[DividendEvent] = []
    excluded: list[ExcludedEvent] = []

    for event in events:
        if event.code != holding.code:
            continue
        if event.is_future:
            # 待收不参与历史归集。这里用 continue 而不是记进 excluded：
            # excluded 的语义是"因为不满足归属条件而被丢弃"，而待收是
            # **被另一个区块接管**，不是被丢弃。
            continue
        if event.ex_date >= holding.start_date:
            included.append(event)
        else:
            excluded.append(
                ExcludedEvent(
                    code=event.code,
                    ex_date=event.ex_date,
                    start_date=holding.start_date,
                    reason=(
                        f"除权除息日 {event.ex_date.isoformat()} 早于开始持有日 "
                        f"{holding.start_date.isoformat()}，该次分红不属于本次持仓"
                    ),
                    dividend_per_share=event.dividend_per_share,
                )
            )

    included.sort(key=lambda item: (item.ex_date, item.source))
    excluded.sort(key=lambda item: item.ex_date)
    return included, excluded


def build_ledger_entry(holding: Holding, event: DividendEvent) -> LedgerEntry:
    """由一条持仓与一条事件算出明细条目。"""
    shares = holding.shares
    cash_amount = event.dividend_per_share * shares
    bonus_shares = event.per_share_bonus * shares
    transfer_shares = event.per_share_transfer * shares

    cost_basis = holding.cost_basis
    missing: list[str] = []

    if cost_basis is None or cost_basis == ZERO:
        yield_on_cost: Decimal | None = None
        missing.append(MISSING_COST_PER_SHARE)
    else:
        yield_on_cost = cash_amount / cost_basis

    if event.close_prev is None or event.close_prev == ZERO:
        dividend_yield: Decimal | None = None
        missing.append(MISSING_CLOSE_PREV)
    else:
        dividend_yield = event.dividend_per_share / event.close_prev

    return LedgerEntry(
        code=holding.code,
        name=holding.name,
        ex_date=event.ex_date,
        effective_date=event.effective_date,
        date_basis=event.date_basis,
        shares_at_event=shares,
        dividend_per_share=event.dividend_per_share,
        cash_amount=cash_amount,
        bonus_shares=bonus_shares,
        transfer_shares=transfer_shares,
        cost_basis=cost_basis,
        yield_on_cost=yield_on_cost,
        dividend_yield=dividend_yield,
        # 一阶段税率为可配置规则位且默认不启用 → 参考净额保持 None（SPEC §4.3）
        tax_reference=None,
        source=event.source,
        close_prev=event.close_prev,
        missing_inputs=tuple(missing),
    )


def build_ledger(
    holdings: Sequence[Holding], events: Sequence[DividendEvent]
) -> Ledger:
    """把持仓与事件合成完整明细账。

    Raises:
        ValueError: 传入的持仓含重复 ``code``（调用方应先归一化）。
    """
    by_code: dict[str, Holding] = {}
    for holding in holdings:
        if holding.code in by_code:
            raise ValueError(
                f"持仓中 {holding.code} 出现多次；应先按 code 归一化为一条"
            )
        by_code[holding.code] = holding

    entries: list[LedgerEntry] = []
    excluded: list[ExcludedEvent] = []
    missing: list[MissingInput] = []
    basis_counts: dict[str, int] = {}

    ordered_holdings = sorted(by_code.values(), key=lambda item: item.code)

    for holding in ordered_holdings:
        owned, dropped = filter_events_for_holding(holding, events)
        excluded.extend(dropped)

        if holding.cost_basis is None or holding.cost_basis == ZERO:
            missing.append(
                MissingInput(
                    code=holding.code,
                    kind=MISSING_COST_PER_SHARE,
                    detail=_DESCRIPTION_BY_KIND[MISSING_COST_PER_SHARE],
                    affected_field=_IMPACT_BY_KIND[MISSING_COST_PER_SHARE],
                )
            )

        holding_needs_price = False
        for event in owned:
            entry = build_ledger_entry(holding, event)
            entries.append(entry)
            basis_counts[entry.date_basis.value] = (
                basis_counts.get(entry.date_basis.value, 0) + 1
            )
            if MISSING_CLOSE_PREV in entry.missing_inputs:
                holding_needs_price = True

        if holding_needs_price:
            missing.append(
                MissingInput(
                    code=holding.code,
                    kind=MISSING_CLOSE_PREV,
                    detail=_DESCRIPTION_BY_KIND[MISSING_CLOSE_PREV],
                    affected_field=_IMPACT_BY_KIND[MISSING_CLOSE_PREV],
                )
            )

    entries.sort(key=lambda item: (item.effective_date, item.code, item.ex_date))
    excluded.sort(key=lambda item: (item.ex_date, item.code))

    return Ledger(
        entries=tuple(entries),
        excluded=tuple(excluded),
        missing_inputs=tuple(missing),
        holdings_used=tuple(ordered_holdings),
        events_considered=len(events),
        date_basis_counts=basis_counts,
    )


def total_cash(entries: Iterable[LedgerEntry]) -> Decimal:
    """明细账的税前现金合计（精确累加，不做中间舍入）。"""
    total = ZERO
    for entry in entries:
        total += entry.cash_amount
    return total


def describe_date_basis(counts: Mapping[str, int]) -> str:
    """把归集依据统计转成一句人话，用于报告与对话输出。"""
    if not counts:
        return "无事件"
    parts = []
    labels = {
        DateBasis.EX_DATE.value: "除权除息日",
        DateBasis.PAY_DATE_DECLARED.value: "数据源声明的派息到账日",
        DateBasis.PAY_DATE_OVERRIDE.value: "人工覆盖的派息到账日",
    }
    for key, count in sorted(counts.items()):
        parts.append(f"{labels.get(key, key)} {count} 条")
    return "；".join(parts)


def rows_to_missing_labels(items: Iterable[MissingInput]) -> list[str]:
    """缺失输入的展示标签列表，如 ``["600519.SH.cost_per_share"]``。"""
    return [item.label() for item in items]


def cash_total_text(entries: Iterable[LedgerEntry]) -> str:
    """税前现金合计的对外字符串。"""
    return format_money(total_cash(entries))
