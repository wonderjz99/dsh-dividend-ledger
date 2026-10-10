"""三分区汇总（SPEC §6.3）。

输出结构::

    report.json
    ├── meta      本次运行的依据、输入、缺失项、去重统计
    ├── totals    全部合计
    ├── by_year   按"归集日期"的年份，年内再拆股票
    ├── by_month  按"归集日期"的年月
    └── by_stock  跨年累计

**归集日期**取决于每条记录的 ``date_basis``：有到账日用得到账日，否则用除权除息日
（SPEC §4.1）。报告永远在 ``meta.date_basis`` 里说清用的是哪一种。

**成本口径的重要说明**：由于一阶段假设持有期内股数恒定，成本基数是**存量**而非**流量**，
所以

- ``by_stock``：成本基数就是该股持有成本；跨股票相加有意义，等于总投入成本。
- ``by_year``：某年的成本基数是"该年有分红的那几只股票的全部持有成本"，
  是**该分区的分母**，**不可跨年相加**（同一只股票会在多个年份重复出现）。
- ``by_month``：与 ``by_year`` 同理，只输出金额，不输出需要分母的比率。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from dividend_ledger.decimals import ZERO, format_money, format_ratio, format_shares
from dividend_ledger.ledger import Ledger, MissingInput, describe_date_basis
from dividend_ledger.models import Holding, LedgerEntry

__all__ = [
    "Aggregate",
    "ReportContext",
    "build_report",
]

#: ``by_year`` / ``by_month`` 的排序方向：最新在前，符合"看账"习惯
_DESCENDING = True


@dataclass
class Aggregate:
    """一个分区的聚合累加器。"""

    cash_amount: Decimal = ZERO
    #: 归集依据 → 条数
    basis_counts: dict[str, int] = field(default_factory=dict)
    #: 分母（成本基数）；``None`` 表示该分区内没有任何带成本的股票
    cost_basis: Decimal | None = None
    #: 加权股息率的分母：Σ(股数 × 除权除息日前收盘价)，即按股数加权的市值
    market_value: Decimal = ZERO
    #: 加权股息率的分子：**只统计有价格的那些事件**的现金，与分母同覆盖范围
    priced_cash: Decimal = ZERO
    #: 有多少条记录带价格、多少条缺价格——缺价格就不能假装股息率完整
    priced_entries: int = 0
    unpriced_entries: int = 0
    bonus_shares: Decimal = ZERO
    transfer_shares: Decimal = ZERO
    event_count: int = 0
    #: 该分区内出现过的股票代码（有序）
    codes: list[str] = field(default_factory=list)
    _code_set: set[str] = field(default_factory=set, repr=False)

    def add_entry(self, entry: LedgerEntry) -> None:
        self.cash_amount += entry.cash_amount
        self.bonus_shares += entry.bonus_shares
        self.transfer_shares += entry.transfer_shares
        self.event_count += 1
        basis = entry.date_basis.value
        self.basis_counts[basis] = self.basis_counts.get(basis, 0) + 1
        if entry.dividend_yield is not None and entry.close_prev is not None:
            # 按【股数】加权，而不是把每股派息与股价直接相加：
            #   股息率 = Σ(每股派息 × 股数) / Σ(收盘价 × 股数) = 现金 ÷ 除权前市值
            # 直接 Σ每股派息 / Σ收盘价 会让 1 股茅台和 1 股高速等价，严重低估组合股息率。
            self.market_value += entry.close_prev * entry.shares_at_event
            self.priced_cash += entry.cash_amount
            self.priced_entries += 1
        else:
            self.unpriced_entries += 1
        if entry.code not in self._code_set:
            self._code_set.add(entry.code)
            self.codes.append(entry.code)

    def add_cost(self, cost: Decimal | None) -> None:
        """把一条持仓成本计入分母（同一分区内每只股票只应计入一次）。"""
        if cost is None:
            return
        self.cost_basis = cost if self.cost_basis is None else self.cost_basis + cost

    @property
    def yield_on_cost(self) -> Decimal | None:
        """成本收益率 = 分区内税前分红合计 ÷ 分区成本基数。"""
        if self.cost_basis is None or self.cost_basis == ZERO:
            return None
        return self.cash_amount / self.cost_basis

    @property
    def dividend_yield(self) -> Decimal | None:
        """股息率 = 有价格事件的现金 ÷ 对应的除权前市值（按股数加权）。

        分子分母必须同覆盖范围：只算有价格的那些事件，否则会出现
        "分子含全部现金、分母只含有价格的市值"这种被夸大的比率。
        """
        if self.market_value == ZERO:
            return None
        return self.priced_cash / self.market_value

    @property
    def price_complete(self) -> bool:
        """该分区内是否每个事件都有价格——否则股息率只覆盖了一部分。"""
        return self.unpriced_entries == 0

    def to_dict(self, *, include_ratio: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "cash_amount": format_money(self.cash_amount),
            "event_count": self.event_count,
            "stock_count": len(self.codes),
            "basis_counts": dict(sorted(self.basis_counts.items())),
        }
        if include_ratio:
            result["cost_basis"] = (
                None if self.cost_basis is None else format_money(self.cost_basis)
            )
            result["yield_on_cost"] = (
                None if self.yield_on_cost is None else format_ratio(self.yield_on_cost)
            )
            result["dividend_yield"] = (
                None
                if self.dividend_yield is None
                else format_ratio(self.dividend_yield)
            )
            result["dividend_yield_complete"] = self.price_complete
            if not self.price_complete and self.priced_entries > 0:
                result["dividend_yield_note"] = (
                    f"该分区有 {self.unpriced_entries} 笔事件缺少除权前收盘价，"
                    f"股息率只覆盖了 {self.priced_entries} 笔"
                )
        result["bonus_shares"] = format_shares(self.bonus_shares)
        result["transfer_shares"] = format_shares(self.transfer_shares)
        return result


@dataclass(frozen=True, slots=True)
class ReportContext:
    """构建报告时与业务无关的元信息。"""

    generated_at: str
    inputs: Mapping[str, Any]
    audit: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    #: 待收分红（阶段三）。由调用方从**原始事件**算出——报告只归集已实施的部分，
    #: 拿不到未实施事件，所以必须从外面传进来。
    upcoming: "UpcomingSummary | None" = None


def _holding_cost(holding: Holding) -> Decimal | None:
    basis = holding.cost_basis
    if basis is None or basis == ZERO:
        return None
    return basis


def build_report(
    ledger: Ledger,
    *,
    context: ReportContext,
) -> dict[str, Any]:
    """把明细账汇总成三分区报告。"""
    entries = list(ledger.entries)
    holdings_by_code = {h.code: h for h in ledger.holdings_used}

    totals = Aggregate()
    by_year: dict[str, Aggregate] = {}
    by_month: dict[str, Aggregate] = {}
    by_stock: dict[str, Aggregate] = {}
    year_stock: dict[tuple[str, str], Aggregate] = {}

    for entry in entries:
        totals.add_entry(entry)

        year = entry.effective_date.year
        month = entry.effective_date.strftime("%Y-%m")
        year_key = str(year)

        by_year.setdefault(year_key, Aggregate()).add_entry(entry)
        by_month.setdefault(month, Aggregate()).add_entry(entry)
        by_stock.setdefault(entry.code, Aggregate()).add_entry(entry)
        year_stock.setdefault((year_key, entry.code), Aggregate()).add_entry(entry)

    # 分母只按"出现过的股票"计入一次，避免逐事件重复累加
    for code in totals.codes:
        totals.add_cost(_holding_cost(holdings_by_code[code]))
    for year_key, aggregate in by_year.items():
        for code in aggregate.codes:
            aggregate.add_cost(_holding_cost(holdings_by_code[code]))
    for code, aggregate in by_stock.items():
        aggregate.add_cost(_holding_cost(holdings_by_code[code]))
    for (year_key, code), aggregate in year_stock.items():
        aggregate.add_cost(_holding_cost(holdings_by_code[code]))

    def sort_years(keys: Iterable[str]) -> list[str]:
        return sorted(keys, reverse=_DESCENDING)

    year_blocks: list[dict[str, Any]] = []
    for year_key in sort_years(by_year):
        block = {"year": year_key, **by_year[year_key].to_dict()}
        slices = [
            {"code": code, "name": _name_of(holdings_by_code, code), **aggregate.to_dict()}
            for (y, code), aggregate in year_stock.items()
            if y == year_key
        ]
        slices.sort(key=lambda item: item["cash_amount"], reverse=True)
        block["by_stock"] = slices
        year_blocks.append(block)

    month_blocks = [
        {"month": month, **by_month[month].to_dict(include_ratio=False)}
        for month in sorted(by_month, reverse=_DESCENDING)
    ]

    stock_blocks = [
        {
            "code": code,
            "name": _name_of(holdings_by_code, code),
            "start_date": holdings_by_code[code].start_date.isoformat(),
            **by_stock[code].to_dict(),
        }
        for code in sorted(
            by_stock, key=lambda c: by_stock[c].cash_amount, reverse=True
        )
    ]

    basis_counts = dict(ledger.date_basis_counts)
    report: dict[str, Any] = {
        "format_version": "1",
        "meta": {
            "generated_at": context.generated_at,
            "generator": "dividend-ledger",
            "date_basis": _dominant_basis(basis_counts),
            "date_basis_detail": describe_date_basis(basis_counts),
            "date_basis_counts": dict(sorted(basis_counts.items())),
            "inputs": dict(context.inputs),
            "missing_inputs": [item.label() for item in ledger.missing_inputs],
            "missing_input_details": [item.to_dict() for item in ledger.missing_inputs],
            "audit": dict(context.audit),
            "warnings": list(context.warnings),
            "notes": _notes(ledger),
        },
        "totals": totals.to_dict(),
        "by_year": year_blocks,
        "by_month": month_blocks,
        "by_stock": stock_blocks,
        # 待收单列。**它的金额绝不进入 totals**——混进去"我已经收了多少"就失真了。
        # 没有待收时是空数组，不是报错（阶段三 C5）。
        "upcoming": (
            context.upcoming.to_dict()
            if context.upcoming is not None
            else {
                "eventCount": 0,
                "stockCount": 0,
                "confirmedAmount": "0.00",
                "proposedAmount": "0.00",
                "totalAmount": "0.00",
                "rows": [],
                "byMonth": [],
                "byStock": [],
            }
        ),
    }
    return report


def _name_of(holdings_by_code: Mapping[str, Holding], code: str) -> str | None:
    holding = holdings_by_code.get(code)
    return holding.name if holding is not None else None


def _dominant_basis(basis_counts: Mapping[str, int]) -> str:
    """报告主归集依据：条数最多者；并列时优先人工覆盖，其次数据源声明。"""
    if not basis_counts:
        return "ex_date"
    priority = {"pay_date_override": 2, "pay_date_declared": 1, "ex_date": 0}
    return max(
        basis_counts,
        key=lambda key: (basis_counts[key], priority.get(key, 0)),
    )


def _notes(ledger: Ledger) -> list[str]:
    """报告里必须原样呈现的口径说明——不允许把依据藏起来。"""
    notes = [
        "主口径为税前现金分红；红利税仅作参考净额，未计入上方任何数字。",
        "分红归属规则：除权除息日不早于开始持有日的事件才计入本次持仓。",
        "一阶段假设持有期内股数恒定；送股/转增只作为额外获得股数列出，"
        "不反向增加后续事件的股数。",
    ]
    if ledger.date_basis_counts.get("ex_date"):
        notes.append(
            "A 股公开数据源不提供派息到账日，按除权除息日归集；"
            "如需真实到账日，可在 data/pay_dates.json 中人工覆盖。"
        )
    if ledger.excluded:
        notes.append(
            f"有 {len(ledger.excluded)} 条事件因除权除息日早于开始持有日被排除，"
            "见 ledger.json 的 excluded_events。"
        )
    return notes


def summarize_missing(missing: Sequence[MissingInput]) -> list[str]:
    """缺失输入的标签列表。"""
    return [item.label() for item in missing]
