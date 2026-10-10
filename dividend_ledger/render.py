"""对话/终端友好的文字渲染。

JSON 是权威产物；这里只负责把关键数字转成人能一眼看懂的一段话，
**刻意不渲染完整 JSON**——账本可能上千条，塞进对话上下文是浪费。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Sequence

from dividend_ledger.decimals import format_ratio  # noqa: F401  (保留：供上层复用)
from dividend_ledger.ledger import Ledger
from dividend_ledger.models import Holding

__all__ = [
    "render_holdings_text",
    "render_import_text",
    "render_report_text",
    "render_money",
]


def render_money(value: Any) -> str:
    """金额展示：``¥ 41,880.00``。"""
    if value is None:
        return "—"
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    return f"¥ {amount:,.2f}"


def _pct(value: Any) -> str:
    """比率展示为百分数；``None`` 显示为 ``—``（不假装是 0）。"""
    if value is None:
        return "—"
    ratio = value if isinstance(value, Decimal) else Decimal(str(value))
    return f"{(ratio * 100).quantize(Decimal('0.01')):f}%"


def render_holdings_text(holdings: Sequence[Holding]) -> str:
    """持仓列表文本。"""
    if not holdings:
        return "持仓为空。请先用 set-holdings 写入持仓。"

    lines = [f"持仓 {len(holdings)} 只：", ""]
    lines.append(f"{'代码':<12}{'名称':<10}{'开始持有':<13}{'股数':>12}{'每股成本':>12}")
    lines.append("-" * 62)

    missing_cost: list[str] = []
    for holding in holdings:
        name = holding.name or "—"
        cost = (
            "—"
            if holding.cost_per_share is None
            else f"{holding.cost_per_share:f}"
        )
        if holding.cost_per_share is None:
            missing_cost.append(holding.code)
        lines.append(
            f"{holding.code:<12}{name:<12}{holding.start_date.isoformat():<13}"
            f"{holding.shares:>12f}{cost:>12}"
        )

    if missing_cost:
        lines.append("")
        lines.append(
            f"注意：{', '.join(missing_cost)} 未提供每股成本，"
            "成本收益率将显示为 —（不会用 0 冒充）。"
        )
    return "\n".join(lines)


def render_import_text(audit: Mapping[str, Any], *, label: str) -> str:
    """导入审计文本。"""
    lines = [
        f"{label}导入完成：",
        f"  文件内条数   {audit.get('items_in_file', 0)}",
        f"  新增         {audit.get('imported', 0)}",
        f"  覆盖         {audit.get('replaced', 0)}",
        f"  未变化       {audit.get('unchanged', 0)}",
        f"  拒绝         {audit.get('rejected_count', 0)}",
    ]
    rejected = audit.get("rejected") or []
    if rejected:
        lines.append("")
        lines.append("被拒绝的记录：")
        for item in rejected[:10]:
            lines.append(f"  [{item.get('index')}] {item.get('reason')}")
        if len(rejected) > 10:
            lines.append(f"  …还有 {len(rejected) - 10} 条，见 JSON 输出")
    return "\n".join(lines)


def render_report_text(report: Mapping[str, Any], *, ledger: Ledger | None = None) -> str:
    """报告摘要文本。

    只回关键数字与文件路径；明细走文件，不进对话。
    """
    meta = report.get("meta", {})
    totals = report.get("totals", {})

    lines: list[str] = []
    lines.append("分红账（" + str(meta.get("date_basis_detail", "")) + "）")
    lines.append("")
    lines.append(f"  税前分红合计   {render_money(totals.get('cash_amount'))}")
    lines.append(f"  持有成本       {render_money(totals.get('cost_basis'))}")
    lines.append(f"  成本收益率     {_pct(_as_decimal(totals.get('yield_on_cost')))}")
    lines.append(f"  股息率         {_pct(_as_decimal(totals.get('dividend_yield')))}{_partial(totals)}")
    lines.append(f"  持股数         {totals.get('stock_count', 0)} 只")
    lines.append(f"  分红事件       {totals.get('event_count', 0)} 笔")
    bonus = totals.get("bonus_shares")
    transfer = totals.get("transfer_shares")
    if _nonzero(bonus) or _nonzero(transfer):
        lines.append(f"  额外获得股数   送股 {bonus} + 转增 {transfer}")

    stock_blocks = report.get("by_stock") or []
    if stock_blocks:
        lines.append("")
        lines.append(
        f"  {'代码':<12}{'名称':<10}{'累计分红':>16}{'成本收益率':>11}{'股息率':>9}{'笔数':>5}"
    )
        for block in stock_blocks:
            lines.append(
                f"  {block.get('code', ''):<12}"
                f"{(block.get('name') or '—'):<10}"
                f"{render_money(block.get('cash_amount')):>16}"
                f"{_pct(_as_decimal(block.get('yield_on_cost'))):>11}"
                f"{_pct(_as_decimal(block.get('dividend_yield'))):>9}"
                f"{block.get('event_count', 0):>5}"
            )

    missing = meta.get("missing_inputs") or []
    if missing:
        lines.append("")
        lines.append("  缺数据：" + "、".join(missing))

    if ledger is not None and ledger.excluded:
        lines.append(
            f"  另有 {len(ledger.excluded)} 条事件因除权除息日早于开始持有日被排除"
        )

    warnings = meta.get("warnings") or []
    if warnings:
        lines.append("")
        for warning in warnings:
            lines.append(f"  警告：{warning}")

    lines.append("")
    lines.append("  口径：主口径税前；红利税未计入；" + _basis_sentence(meta))
    return "\n".join(lines)


def _partial(aggregate: Mapping[str, Any]) -> str:
    """股息率只覆盖部分事件时，明确标注——不允许假装完整。"""
    if aggregate.get("dividend_yield_complete") is False:
        return "  ← 部分覆盖"
    return ""


def _basis_sentence(meta: Mapping[str, Any]) -> str:
    dominant = meta.get("date_basis")
    if dominant == "pay_date_override":
        return "归集日期使用人工覆盖的派息到账日"
    if dominant == "pay_date_declared":
        return "归集日期使用数据源声明的派息到账日"
    return "A 股无派息到账日，按除权除息日归集"


def _nonzero(value: Any) -> bool:
    if value is None:
        return False
    try:
        return Decimal(str(value)) != 0
    except Exception:  # pragma: no cover - 仅防御
        return True


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # pragma: no cover - 仅防御
        return None


def format_ratio_percent(value: Decimal | None) -> str:
    """供其它模块复用的百分数格式化。"""
    if value is None:
        return "—"
    return f"{(value * 100).quantize(Decimal('0.01')):f}%"
