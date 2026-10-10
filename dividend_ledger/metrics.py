"""界面用的派生指标。

两个股息率，**口径完全不同，不能混用**：

======================  ==============================================  ================
指标                    公式                                            何时为 None
======================  ==============================================  ================
动态股息率              近 12 个月已实施每股派息合计 ÷ 最新收盘价        缺价格，或近 12 个月没分红
预期股息率              已公告未实施的每股派息合计 ÷ 最新收盘价          缺价格，或没有未实施方案
======================  ==============================================  ================

**"最近一笔"不算 TTM。** 一只股票可能一年内派两次（宁德时代就有），
只取最后一笔会低估动态股息率，所以必须按**除权除息日**归到 12 个月窗口里再求和。

**预期股息率只算已公告的**，不做任何按历史规律的外推——这是 SPEC §4.7 定的红线。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Sequence

from dividend_ledger.decimals import ZERO, format_money, format_per_share, format_price, format_ratio
from dividend_ledger.models import DividendEvent, DividendStatus, Holding

__all__ = [
    "PortfolioSummary",
    "StockMetrics",
    "compute_metrics",
    "compute_portfolio",
    "trailing_twelve_months",
]

#: TTM 窗口长度（按自然年回退，A 股分红频率以年为主，够用且口径简单）
TTM_DAYS = 365


def trailing_twelve_months(reference: date) -> date:
    """返回 TTM 窗口的起始日（含）。"""
    return reference - timedelta(days=TTM_DAYS)


@dataclass(frozen=True, slots=True)
class StockMetrics:
    """一只股票在界面上要显示的派生指标。"""

    code: str
    name: str
    price: Decimal | None
    #: 近 12 个月已实施的每股派息合计
    ttm_per_share: Decimal
    ttm_event_count: int
    #: 动态股息率 = ttm_per_share ÷ price
    dynamic_yield: Decimal | None
    #: 已公告未实施的每股派息合计
    planned_per_share: Decimal
    planned_event_count: int
    #: 预期股息率 = planned_per_share ÷ price
    expected_yield: Decimal | None
    #: 下一笔待收的除权除息日
    next_ex_date: date | None
    next_plan_text: str | None
    #: 已持有的股数与成本收益率（未持有时为 None）
    shares: Decimal | None
    cost_basis: Decimal | None
    yield_on_cost: Decimal | None
    #: 该股在账本里累计已收分红（面向已持有）
    received_total: Decimal
    missing: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """转成客户端要的 JSON（数值一律字符串，与账本口径一致）。"""
        return {
            "code": self.code,
            "name": self.name,
            "price": None if self.price is None else format_price(self.price),
            "ttmPerShare": format_per_share(self.ttm_per_share),
            "ttmEventCount": self.ttm_event_count,
            "dynamicYield": (
                None if self.dynamic_yield is None else format_ratio(self.dynamic_yield)
            ),
            "plannedPerShare": format_per_share(self.planned_per_share),
            "plannedEventCount": self.planned_event_count,
            "expectedYield": (
                None if self.expected_yield is None else format_ratio(self.expected_yield)
            ),
            "nextExDate": None if self.next_ex_date is None else self.next_ex_date.isoformat(),
            "nextPlanText": self.next_plan_text,
            "shares": None if self.shares is None else format_per_share(self.shares),
            "costBasis": None if self.cost_basis is None else format_money(self.cost_basis),
            "yieldOnCost": (
                None if self.yield_on_cost is None else format_ratio(self.yield_on_cost)
            ),
            "receivedTotal": format_money(self.received_total),
            "missing": list(self.missing),
        }


def compute_metrics(
    *,
    code: str,
    name: str,
    price: Decimal | None,
    events: Sequence[DividendEvent],
    holding: Holding | None = None,
    reference: date | None = None,
) -> StockMetrics:
    """由价格、事件与（可选）持仓算出界面指标。

    Args:
        events: 该股票的**全部**已抓事件（含未实施）。函数内部按状态与窗口切分。
        holding: 若已持有，用于算成本收益率与累计已收。
        reference: "今天"，便于测试固定。
    """
    today = reference or date.today()
    window_start = trailing_twelve_months(today)

    ttm_per_share = ZERO
    ttm_count = 0
    planned_per_share = ZERO
    planned_count = 0
    next_ex_date: date | None = None
    next_plan_text: str | None = None

    for event in events:
        if event.status is DividendStatus.IMPLEMENTED:
            if window_start <= event.ex_date <= today:
                ttm_per_share += event.dividend_per_share
                ttm_count += 1
        else:
            # 已公告未实施（含预案）——只做承诺性展示，不进历史统计
            planned_per_share += event.dividend_per_share
            planned_count += 1
            if next_ex_date is None or event.ex_date < next_ex_date:
                next_ex_date = event.ex_date
                next_plan_text = event.plan_text

    missing: list[str] = []
    dynamic_yield: Decimal | None = None
    if price is None or price <= ZERO:
        missing.append("price")
    elif ttm_per_share > ZERO:
        dynamic_yield = ttm_per_share / price
    else:
        missing.append("ttm_dividend")

    expected_yield: Decimal | None = None
    if price is None or price <= ZERO:
        pass  # price 已在 missing 里
    elif planned_per_share > ZERO:
        expected_yield = planned_per_share / price
    else:
        missing.append("planned_dividend")

    shares: Decimal | None = None
    cost_basis: Decimal | None = None
    yield_on_cost: Decimal | None = None
    received_total = ZERO

    if holding is not None:
        shares = holding.shares
        cost_basis = holding.cost_basis
        received_total = sum(
            (
                event.dividend_per_share * holding.shares
                for event in events
                if event.status is DividendStatus.IMPLEMENTED
                and event.ex_date >= holding.start_date
            ),
            ZERO,
        )
        if cost_basis is not None and cost_basis > ZERO and received_total > ZERO:
            yield_on_cost = received_total / cost_basis
        elif cost_basis is None:
            missing.append("cost_per_share")

    return StockMetrics(
        code=code,
        name=name,
        price=price,
        ttm_per_share=ttm_per_share,
        ttm_event_count=ttm_count,
        dynamic_yield=dynamic_yield,
        planned_per_share=planned_per_share,
        planned_event_count=planned_count,
        expected_yield=expected_yield,
        next_ex_date=next_ex_date,
        next_plan_text=next_plan_text,
        shares=shares,
        cost_basis=cost_basis,
        yield_on_cost=yield_on_cost,
        received_total=received_total,
        missing=tuple(missing),
    )


def group_events_by_code(
    events: Iterable[DividendEvent],
) -> dict[str, list[DividendEvent]]:
    """按代码分组事件，便于逐股算指标。"""
    grouped: dict[str, list[DividendEvent]] = {}
    for event in events:
        grouped.setdefault(event.code, []).append(event)
    return grouped


# --------------------------------------------------------------------------- #
# 组合层面：整个持仓的总股息率
# --------------------------------------------------------------------------- #
#
# 口径按**市值加权**，不是把各股股息率简单平均——后者会让 100 股的茅台和
# 1000 股的银行同等影响结果，是错的。
#
#   组合动态股息率 = Σ(每股 TTM 派息 × 持股数) ÷ Σ(最新价 × 持股数)
#                 = Σ(该股近 12 个月已派现金) ÷ 持仓市值
#   组合预期股息率 = Σ(已公告未实施每股派息 × 持股数) ÷ 持仓市值
#
# 分母只统计**有价格**的持仓；有股票取不到价时，total 会标 incomplete 并把
# 覆盖了多少市值写清楚，绝不假装分母是完整的。


@dataclass(frozen=True, slots=True)
class PortfolioSummary:
    """整个持仓的合计与总股息率。"""

    stock_count: int
    priced_count: int
    market_value: Decimal
    #: 按份额计算的动态分红现金（Σ TTM 每股派息 × 股数）
    dynamic_cash: Decimal
    #: 按份额计算的预期分红现金（Σ 已公告每股派息 × 股数）
    planned_cash: Decimal
    #: 持仓成本合计（有成本的股票才计入）
    cost_basis: Decimal | None
    #: 累计已收（已实施且晚于开始持有日）
    received_total: Decimal
    dynamic_yield: Decimal | None
    expected_yield: Decimal | None
    yield_on_cost: Decimal | None
    #: 市值是否覆盖了全部持仓
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "stockCount": self.stock_count,
            "pricedCount": self.priced_count,
            "marketValue": format_money(self.market_value),
            "dynamicCash": format_money(self.dynamic_cash),
            "plannedCash": format_money(self.planned_cash),
            "costBasis": None if self.cost_basis is None else format_money(self.cost_basis),
            "receivedTotal": format_money(self.received_total),
            "dynamicYield": (
                None if self.dynamic_yield is None else format_ratio(self.dynamic_yield)
            ),
            "expectedYield": (
                None if self.expected_yield is None else format_ratio(self.expected_yield)
            ),
            "yieldOnCost": (
                None if self.yield_on_cost is None else format_ratio(self.yield_on_cost)
            ),
            "complete": self.complete,
        }


def compute_portfolio(
    stock_metrics: Sequence[StockMetrics],
    holdings: Sequence[Holding],
) -> PortfolioSummary:
    """把逐股指标聚成整个持仓的合计。

    Args:
        stock_metrics: 每只**已持有**股票的指标（未持有的不该传进来）。
        holdings: 对应持仓，用来取股数、成本与开始持有日。
    """
    holding_by_code = {item.code: item for item in holdings}

    market_value = ZERO
    dynamic_cash = ZERO
    planned_cash = ZERO
    received_total = ZERO
    cost_basis = ZERO
    cost_seen = False
    priced_count = 0

    for metrics in stock_metrics:
        holding = holding_by_code.get(metrics.code)
        if holding is None:
            continue
        shares = holding.shares

        dynamic_cash += metrics.ttm_per_share * shares
        planned_cash += metrics.planned_per_share * shares
        received_total += metrics.received_total
        if metrics.price is not None and metrics.price > ZERO:
            market_value += metrics.price * shares
            priced_count += 1
        if holding.cost_basis is not None:
            cost_basis += holding.cost_basis
            cost_seen = True

    complete = priced_count == len([m for m in stock_metrics if holding_by_code.get(m.code)])

    return PortfolioSummary(
        stock_count=len(stock_metrics),
        priced_count=priced_count,
        market_value=market_value,
        dynamic_cash=dynamic_cash,
        planned_cash=planned_cash,
        cost_basis=cost_basis if cost_seen else None,
        received_total=received_total,
        dynamic_yield=(
            dynamic_cash / market_value if market_value > ZERO and dynamic_cash > ZERO else None
        ),
        expected_yield=(
            planned_cash / market_value if market_value > ZERO and planned_cash > ZERO else None
        ),
        yield_on_cost=(
            received_total / cost_basis
            if cost_seen and cost_basis > ZERO and received_total > ZERO
            else None
        ),
        complete=complete,
    )
