"""待收分红（已公告未实施）—— 阶段三。

**这是全项目最需要小心的地方**：三种东西性质完全不同，混在一起就失真了。

===================  ==========================================  ==================
类别                 例子                                        处理
===================  ==========================================  ==================
① 已公告未实施       "10派25.911元，除权除息日 2026-06-19"      确定性计算，标 confirmed
② 董事会预案         "10派25元（预案），待股东大会审议"          可列，标 proposed，明示可能被否
③ 按历史规律外推     "连续 5 年 6 月分红，明年大概也…"           **不做**——这是预测
===================  ==========================================  ==================

**铁律**：①② 的金额**绝不进入历史累计** `totals.cash_amount`。一旦混进去，
"我已经收了多少"就失真了。这条有专门的测试守护
（`tests/test_upcoming.py::NoContaminationTest`）。

另外两个明确的假设：

- 待收金额 = 每股派息 × **当前**股数。未发生送转，所以**不做股数推算**；
  一阶段本来就假设持有期内股数恒定。
- 归月用**除权除息日**所在的月（这是"哪个月要分"最接近的确定日期）。
  若该事件有到账日覆盖（阶段四的人工覆盖表），则改用到账日的月份并标注。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from dividend_ledger.decimals import ZERO, format_money, format_ratio
from dividend_ledger.models import DividendEvent, DividendStatus, Holding

__all__ = [
    "UpcomingRow",
    "UpcomingSummary",
    "build_upcoming",
    "UPCOMING_STATUSES",
]

#: 只有这两个状态算"待收"；IMPLEMENTED 属于历史，绝不在这里
UPCOMING_STATUSES: frozenset[DividendStatus] = frozenset(
    {DividendStatus.CONFIRMED, DividendStatus.PROPOSED}
)


@dataclass(frozen=True, slots=True)
class UpcomingRow:
    """一笔待收分红（某只股票的一个方案）。"""

    code: str
    name: str
    #: 除权除息日
    ex_date: date
    #: 到账日；A 股公开源不提供，只有人工覆盖表能给（阶段四）
    pay_date: date | None
    per_share: Decimal
    shares: Decimal
    cash_amount: Decimal
    status: DividendStatus
    plan_text: str | None
    #: 归月依据：``ex_date`` 或 ``pay_date``
    month_basis: str

    @property
    def month(self) -> str:
        """归属月份 ``YYYY-MM``。"""
        anchor = self.pay_date if self.month_basis == "pay_date" and self.pay_date else self.ex_date
        return f"{anchor.year:04d}-{anchor.month:02d}"

    @property
    def is_proposed(self) -> bool:
        return self.status is DividendStatus.PROPOSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "exDate": self.ex_date.isoformat(),
            "payDate": None if self.pay_date is None else self.pay_date.isoformat(),
            "month": self.month,
            "monthBasis": self.month_basis,
            # 每股金额与股数都给出，用户才能核对"这个数是怎么来的"
            "perShare": format_money(self.per_share),
            "shares": _format_shares(self.shares),
            "cashAmount": format_money(self.cash_amount),
            "status": self.status.value,
            "certain": not self.is_proposed,
            "planText": self.plan_text,
            "payDateKnown": self.pay_date is not None,
        }


@dataclass(frozen=True, slots=True)
class UpcomingSummary:
    """待收合计。**任何字段都不进历史累计。**"""

    rows: tuple[UpcomingRow, ...]
    #: 已公告未实施（确定性）
    confirmed_amount: Decimal
    #: 董事会预案（可能被否/被改）
    proposed_amount: Decimal
    stock_count: int

    @property
    def total_amount(self) -> Decimal:
        """两者之和——界面上要**分开显示**，这个值只用于排序/概览。"""
        return self.confirmed_amount + self.proposed_amount

    @property
    def is_empty(self) -> bool:
        return len(self.rows) == 0

    def by_month(self) -> list[dict[str, Any]]:
        """按预计到账月份分组。"""
        buckets: dict[str, list[UpcomingRow]] = {}
        for row in self.rows:
            buckets.setdefault(row.month, []).append(row)
        return [
            {
                "month": month,
                "cashAmount": format_money(sum((r.cash_amount for r in items), ZERO)),
                "confirmedAmount": format_money(
                    sum((r.cash_amount for r in items if not r.is_proposed), ZERO)
                ),
                "proposedAmount": format_money(
                    sum((r.cash_amount for r in items if r.is_proposed), ZERO)
                ),
                "eventCount": len(items),
                "rows": [r.to_dict() for r in items],
            }
            for month, items in sorted(buckets.items())
        ]

    def by_stock(self) -> list[dict[str, Any]]:
        """按股票分组。"""
        buckets: dict[str, list[UpcomingRow]] = {}
        for row in self.rows:
            buckets.setdefault(row.code, []).append(row)
        result = []
        for code, items in sorted(buckets.items()):
            result.append(
                {
                    "code": code,
                    "name": items[0].name,
                    "cashAmount": format_money(sum((r.cash_amount for r in items), ZERO)),
                    "eventCount": len(items),
                    "rows": [r.to_dict() for r in items],
                }
            )
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "eventCount": len(self.rows),
            "stockCount": self.stock_count,
            "confirmedAmount": format_money(self.confirmed_amount),
            "proposedAmount": format_money(self.proposed_amount),
            "totalAmount": format_money(self.total_amount),
            # 空态是**空数组**，不是报错
            "rows": [row.to_dict() for row in self.rows],
            "byMonth": self.by_month(),
            "byStock": self.by_stock(),
        }


def build_upcoming(
    events: Iterable[DividendEvent],
    holdings: Sequence[Holding],
) -> UpcomingSummary:
    """挑出待收事件并算金额。

    Args:
        events: 全部事件（内部按状态过滤，已实施的一律忽略）。
        holdings: 当前持仓。**不在持仓里的股票不计入**——没持有就没有"待收"。

    Returns:
        待收汇总；没有待收时 ``rows`` 为空元组而不是 ``None``。
    """
    holding_by_code = {item.code: item for item in holdings}
    rows: list[UpcomingRow] = []

    for event in events:
        if event.status not in UPCOMING_STATUSES:
            continue
        holding = holding_by_code.get(event.code)
        if holding is None:
            continue

        # 归月依据：有到账日用它的月份（更贴近"钱哪个月到"），否则用除权除息日
        uses_pay_date = event.pay_date is not None
        rows.append(
            UpcomingRow(
                code=event.code,
                name=holding.name or event.code,
                ex_date=event.ex_date,
                pay_date=event.pay_date,
                per_share=event.dividend_per_share,
                shares=holding.shares,
                cash_amount=event.dividend_per_share * holding.shares,
                status=event.status,
                plan_text=event.plan_text,
                month_basis="pay_date" if uses_pay_date else "ex_date",
            )
        )

    # 排序：先按月份，再按除权日，最后按代码——保证输出可复现
    rows.sort(key=lambda r: (r.month, r.ex_date, r.code, r.per_share))

    confirmed = sum(
        (r.cash_amount for r in rows if r.status is DividendStatus.CONFIRMED), ZERO
    )
    proposed = sum((r.cash_amount for r in rows if r.is_proposed), ZERO)

    return UpcomingSummary(
        rows=tuple(rows),
        confirmed_amount=confirmed,
        proposed_amount=proposed,
        stock_count=len({r.code for r in rows}),
    )


def _format_shares(value: Decimal) -> str:
    """股数去掉无意义的小数尾巴（与 CLI 的显示口径一致）。"""
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return str(normalized.quantize(Decimal(1)))
    return format(normalized, "f")
