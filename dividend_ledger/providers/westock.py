"""westock（腾讯自选股）分红数据解析器 —— 主力数据源。

**一阶段定位**：只实现字段映射与量纲换算 + 离线单测；**不联网、不调用 CLI**。
二阶段才接入真实 ``westock`` 命令。

字段映射（依据 ``docs/westock-skill-reference/references/ai_usage_guide.md:497``）::

    westock A 股字段        →  本包字段                换算
    --------------------------------------------------------------
    exDiviDate             →  ex_date                  —
    rightRegDate           →  right_reg_date           —
    cashDiviRMB            →  dividend_per_share       ÷ 10  ← 量纲陷阱
    bonusShareRatio        →  per_share_bonus          ÷ 10  ← 量纲陷阱
    tranAddShareRatio      →  per_share_transfer       ÷ 10  ← 量纲陷阱
    dividendPlan           →  plan_text                —
    proposalSn             →  proposal_sn              —      ← 同日多方案的唯一键

**A 股字段集里没有派息到账日**（只有港股 ``cashPayDate``、美股 ``payDate``），
所以 A 股记录一律 ``date_basis = ex_date``。

**量纲陷阱**：``cashDiviRMB`` 是"**每 10 股**派息（元）"，不是每股。
依据 ``scenarios-guide.md:515``：*"A股分红数据为『每10股派息』（cashDiviRMB）"*。
已用三重独立验证确认（见 ``docs/westock-verified-behaviour.md`` §6），
其中最强的一条是用 ``totalCashDiviComRMB ÷ (cashDiviRMB ÷ 10)`` 反推总股本，
结果与真实总股本吻合到个位。

**真实输出格式是 Markdown 表格，不是 JSON**（实测，见核对记录 §1）::

    （空行）
    #### sh600519
    （空行）
    **分红历史**
    （空行）
    | bonusShareRatio | cashDiviRMB | ... |
    | --- | --- | ... |
    |  | 239.57 | 是 | ... |
    |  | 280.24 | 是 | ... |

**限频会静默失败**（核对记录 §2）：请求过频时 CLI 把"请求过于频繁，请稍后再试"
写进 stdout，**退出码仍是 0**。若不识别，会被当成"这只股票没有分红"，
于是账本**静默少算分红**——所以这里必须显式抛错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from dividend_ledger.codes import normalize_code, to_cli_code
from dividend_ledger.decimals import ZERO, parse_decimal
from dividend_ledger.errors import DataError, ProviderError
from dividend_ledger.models import DateBasis, DividendEvent, DividendStatus
from dividend_ledger.providers.base import DividendProvider, ProviderResult
from dividend_ledger.store import RejectedItem

__all__ = [
    "KLINE_HEADER",
    "SEARCH_HEADER",
    "make_price_runner",
    "StockMatch",
    "parse_search_markdown",
    "NOT_IMPLEMENTED_KEYWORDS",
    "RATE_LIMIT_MARKERS",
    "WESTOCK_SHARES_BASIS",
    "WestockProvider",
    "WestockRowResult",
    "enrich_close_prev",
    "make_cli_runner",
    "make_kline_runner",
    "parse_dividend_markdown",
    "parse_kline_markdown",
    "parse_stock_block",
]

#: ``westock search`` 的真实表头（照抄实测输出）
SEARCH_HEADER = ("code", "name", "type")

#: ``westock search`` 的真实表头（照抄实测输出）
KLINE_HEADER = (
    "date",
    "open",
    "last",
    "high",
    "low",
    "volume",
    "amount",
    "exchange",
    "change_pct",
)

#: westock 的现金分红与送转比例都是"每 10 股"口径
WESTOCK_SHARES_BASIS = Decimal(10)

#: 出现这些词说明方案还没真正实施，不能计入历史分红
NOT_IMPLEMENTED_KEYWORDS = ("预案", "取消", "终止", "否决", "不分配", "未实施")

#: 限频 / 服务端提示的识别标记。命中即报错，绝不返回空列表。
RATE_LIMIT_MARKERS = ("请求过于频繁", "请稍后", "too many requests", "rate limit")

#: 表头字段顺序（照抄真实输出，用于校验列未错位）
EXPECTED_HEADER = (
    "bonusShareRatio",
    "cashDiviRMB",
    "dividendFlag",
    "dividendPlan",
    "dividendType",
    "exDiviDate",
    "procedure",
    "proposalSn",
    "reportEndDate",
    "rightRegDate",
    "totalCashDiviComRMB",
    "tranAddShareRatio",
)

_STOCK_HEADER_RE = re.compile(r"^#{2,4}\s*(\S+)\s*$")
_ROW_RE = re.compile(r"^\s*\|(.*)\|\s*$")
_SEPARATOR_RE = re.compile(r"^\s*\|(?:\s*:?-{2,}:?\s*\|)+\s*$")


class WestockRowResult:
    """单行解析结果（用类而不用 tuple，避免调用方记错顺序）。"""

    __slots__ = ("event", "skip_reason", "raw")

    def __init__(
        self,
        event: DividendEvent | None = None,
        *,
        skip_reason: str | None = None,
        raw: Mapping[str, Any] | None = None,
    ) -> None:
        self.event = event
        self.skip_reason = skip_reason
        self.raw = raw

    @property
    def skipped(self) -> bool:
        return self.event is None


# --------------------------------------------------------------------------- #
# 标量解析
# --------------------------------------------------------------------------- #


def _parse_compact_date(raw: Any, *, field: str) -> date:
    """解析 ``YYYYMMDD``（真实格式），也接受 ``YYYY-MM-DD``。"""
    if raw is None:
        raise DataError(f"{field} 为空")
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text or text in {"-", "--"}:
        raise DataError(f"{field} 为空值：{raw!r}")

    digits = text.replace("-", "").replace("/", "").replace(".", "")
    if len(digits) == 8 and digits.isdigit():
        try:
            return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            raise DataError(f"{field} 不是合法日期：{raw!r}") from None
    raise DataError(f"{field} 无法解析为日期：{raw!r}；期望 YYYYMMDD")


def _parse_optional_date(raw: Any, *, field: str) -> date | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        return _parse_compact_date(raw, field=field)
    except DataError:
        return None


def _parse_shares_basis_value(raw: Any, *, field: str) -> Decimal:
    """解析"每 10 股"口径的数值并换算成每股口径。

    真实数据里**没有值时是空字符串**（既不是 0 也不是 null），所以空值按 0 处理。
    这是有损转换（分不清"0"与"没数据"），但对送转比例而言两者业务含义相同。
    """
    if raw is None:
        return ZERO
    if isinstance(raw, bool):
        raise DataError(f"{field} 收到布尔值：{raw!r}")
    if isinstance(raw, (int, float)):
        raw = str(raw)
    text = str(raw).strip().replace(",", "")
    if not text or text in {"-", "--", "None", "nan", "NaN"}:
        return ZERO
    try:
        per_ten = Decimal(text)
    except InvalidOperation:
        raise DataError(f"{field} 不是合法数字：{raw!r}") from None
    if not per_ten.is_finite():
        raise DataError(f"{field} 必须是有限数：{raw!r}")
    if per_ten < ZERO:
        raise DataError(f"{field} 不能为负数：{raw!r}")
    return per_ten / WESTOCK_SHARES_BASIS


def _status_from_row(procedure: str, ex_date: date, *, today: date) -> DividendStatus:
    """按 ``procedure`` 文本 + 除权日与今天的关系判定状态。

    真实数据里 ``方案实施`` 的行**也可能带未来的除权日**（核对记录 §5），
    所以不能只看文本。
    """
    if any(keyword in procedure for keyword in NOT_IMPLEMENTED_KEYWORDS):
        return DividendStatus.PROPOSED
    return (
        DividendStatus.IMPLEMENTED
        if ex_date <= today
        else DividendStatus.CONFIRMED
    )


# --------------------------------------------------------------------------- #
# 表格解析
# --------------------------------------------------------------------------- #


def _split_row(line: str) -> list[str]:
    """把 ``| a | b |`` 拆成 ``["a", "b"]``，并去掉每格两端空白。

    注意：真实输出的表头形如 ``| bonusShareRatio | cashDiviRMB |``，
    单元格自带空格，不 strip 会让表头比对与取值全部失配。
    """
    match = _ROW_RE.match(line)
    if match is None:
        raise DataError(f"不是合法的表格行：{line!r}")
    return [cell.strip() for cell in match.group(1).split("|")]


def _row_to_mapping(cells: Sequence[str], header: Sequence[str], *, where: str) -> dict[str, str]:
    if len(cells) != len(header):
        raise DataError(
            f"{where} 列数不匹配：表头 {len(header)} 列，该行 {len(cells)} 列"
        )
    return dict(zip(header, cells))


def parse_stock_block(
    rows: Sequence[Mapping[str, Any]],
    *,
    code_hint: str | None,
    today: date | None = None,
    where: str = "block",
) -> tuple[list[DividendEvent], list[RejectedItem]]:
    """把一只股票的所有表格行解析成事件。

    Args:
        rows: 每行的字段字典（键为 westock 字段名）。
        code_hint: 段头里的代码（如 ``sh600519``）；行内没有代码字段，只能靠它。
        today: 判定状态用的"今天"，便于测试固定。
    """
    reference = today or date.today()
    events: list[DividendEvent] = []
    rejected: list[RejectedItem] = []

    if code_hint is None:
        return [], [
            RejectedItem(
                index=-1,
                reason="分红表格缺少股票代码段头（#### <代码>），无法确定归属",
                payload=list(rows[:1]),
            )
        ]

    try:
        code = normalize_code(code_hint)
    except Exception as exc:
        return [], [
            RejectedItem(index=-1, reason=f"段头代码 {code_hint!r} 非法：{exc}")
        ]

    for index, row in enumerate(rows):
        row_where = f"{where}[{index}]"

        raw_ex = row.get("exDiviDate")
        if raw_ex is None or not str(raw_ex).strip():
            rejected.append(
                RejectedItem(
                    index=index,
                    reason="跳过：缺少除权除息日（exDiviDate）",
                    payload=dict(row),
                )
            )
            continue

        try:
            ex_date = _parse_compact_date(raw_ex, field=f"{row_where}.exDiviDate")
            dividend_per_share = _parse_shares_basis_value(
                row.get("cashDiviRMB"), field=f"{row_where}.cashDiviRMB"
            )
            per_share_bonus = _parse_shares_basis_value(
                row.get("bonusShareRatio"), field=f"{row_where}.bonusShareRatio"
            )
            per_share_transfer = _parse_shares_basis_value(
                row.get("tranAddShareRatio"), field=f"{row_where}.tranAddShareRatio"
            )
        except DataError as exc:
            rejected.append(
                RejectedItem(index=index, reason=str(exc), payload=dict(row))
            )
            continue

        if (
            dividend_per_share == ZERO
            and per_share_bonus == ZERO
            and per_share_transfer == ZERO
        ):
            rejected.append(
                RejectedItem(
                    index=index,
                    reason="跳过：既无现金分红也无送转（可能是纯配股或不分配方案）",
                    payload=dict(row),
                )
            )
            continue

        procedure = str(row.get("procedure") or "").strip()
        proposal_sn = str(row.get("proposalSn") or "").strip() or None
        plan_text = str(row.get("dividendPlan") or "").strip() or None

        events.append(
            DividendEvent(
                code=code,
                ex_date=ex_date,
                dividend_per_share=dividend_per_share,
                # A 股无派息到账日字段 → 只能按除权除息日归集（SPEC §4.1）
                date_basis=DateBasis.EX_DATE,
                pay_date=None,
                per_share_bonus=per_share_bonus,
                per_share_transfer=per_share_transfer,
                right_reg_date=_parse_optional_date(
                    row.get("rightRegDate"), field=f"{row_where}.rightRegDate"
                ),
                # close_prev 不在分红接口里，需另取 K 线；缺则股息率为 None
                close_prev=None,
                plan_text=plan_text,
                proposal_sn=proposal_sn,
                status=_status_from_row(procedure, ex_date, today=reference),
                source="westock",
                source_ref={
                    "ticker": code_hint,
                    "dividendFlag": row.get("dividendFlag"),
                    "procedure": procedure,
                    "dividendType": row.get("dividendType"),
                    "reportEndDate": row.get("reportEndDate"),
                    "raw_cashDiviRMB": row.get("cashDiviRMB"),
                    "raw_bonusShareRatio": row.get("bonusShareRatio"),
                    "raw_tranAddShareRatio": row.get("tranAddShareRatio"),
                    "shares_basis": int(WESTOCK_SHARES_BASIS),
                },
            )
        )

    return events, rejected


def parse_dividend_markdown(
    text: str,
    *,
    today: date | None = None,
    start: date | None = None,
    end: date | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> ProviderResult:
    """解析 ``westock dividend`` 的完整 Markdown 输出。

    Raises:
        ProviderError: 命中限频提示；或同一 ``(代码, 除权日, proposalSn)`` 重复。
        DataError: 输出结构无法识别。
    """
    reference = today or date.today()
    stripped = text.strip()

    if not stripped:
        raise ProviderError("westock 返回了空输出")
    lowered = stripped.lower()
    for marker in RATE_LIMIT_MARKERS:
        if marker.lower() in lowered:
            raise ProviderError(
                f"westock 返回了限频/服务端提示：{stripped.splitlines()[0]!r}。"
                "这**不是**「没有分红数据」——CLI 在限频时退出码仍为 0，"
                "若当成空结果会导致账本静默少算分红。请降低请求频率后重试。"
            )

    lines = stripped.splitlines()
    blocks = _split_stock_blocks(lines)
    if not blocks:
        raise DataError(
            "无法在 westock 输出里找到任何分红表格（期望 `#### <代码>` + Markdown 表格）；"
            f"首行：{lines[0]!r}"
        )

    events: list[DividendEvent] = []
    rejected: list[RejectedItem] = []
    rows_total = 0

    for block_index, (code_hint, rows) in enumerate(blocks):
        rows_total += len(rows)
        parsed, block_rejected = parse_stock_block(
            rows,
            code_hint=code_hint,
            today=reference,
            where=f"block[{block_index}:{code_hint}]",
        )
        events.extend(parsed)
        rejected.extend(block_rejected)

    if start is not None or end is not None:
        events = [
            event
            for event in events
            if (start is None or event.ex_date >= start)
            and (end is None or event.ex_date <= end)
        ]

    seen: dict[tuple[str, str, str], int] = {}
    for index, event in enumerate(events):
        key = event.natural_key
        previous = seen.get(key)
        if previous is not None:
            raise ProviderError(
                f"westock 返回中 {event.code}@{event.ex_date.isoformat()} "
                f"proposalSn={event.proposal_sn or '—'} 出现多次"
                f"（第 {previous + 1} 条与第 {index + 1} 条）；"
                "同一次除权除息的多条方案靠 proposalSn 区分，重复行会重复计数"
            )
        seen[key] = index

    info: dict[str, Any] = {
        "provider": "westock",
        "shares_basis": int(WESTOCK_SHARES_BASIS),
        "stocks_in_payload": len(blocks),
        "rows_in_payload": rows_total,
        "matched_events": len(events),
    }
    if provenance:
        info.update(provenance)

    return ProviderResult(events=tuple(events), rejected=tuple(rejected), provenance=info)


def _split_stock_blocks(lines: Sequence[str]) -> list[tuple[str | None, list[dict[str, str]]]]:
    """按 ``#### <代码>`` 把输出切成若干块，并解析每块的表格。"""
    blocks: list[tuple[str | None, list[dict[str, str]]]] = []
    current_code: str | None = None
    header: tuple[str, ...] | None = None
    rows: list[dict[str, str]] = []
    in_table = False

    def flush() -> None:
        """收尾当前股票段。

        即使一行数据都没有也要落盘：``#### sh600519`` 后面没有表格，
        语义是"这只股票没有分红数据"，而不是"这不是一份分红输出"。
        """
        nonlocal rows, header, in_table
        if current_code is not None:
            blocks.append((current_code, rows))
        rows = []
        header = None
        in_table = False

    for line in lines:
        head = _STOCK_HEADER_RE.match(line)
        if head is not None:
            flush()
            current_code = head.group(1)
            continue

        if not line.strip().startswith("|"):
            continue

        if _SEPARATOR_RE.match(line):
            in_table = True
            continue

        cells = [cell.strip() for cell in _split_row(line)]

        if header is None:
            candidate = tuple(cells)
            if candidate == EXPECTED_HEADER:
                header = candidate
                continue
            # 有段头（说明上游认为这里有分红数据）却不认识这张表 → **必须报错**。
            # 静默跳过会让账本悄悄少算分红；字段错位比报错危险得多。
            if current_code is not None:
                raise DataError(
                    "westock 分红表头与预期不一致（字段可能已变更）：\n"
                    f"  期望：{list(EXPECTED_HEADER)}\n"
                    f"  实际：{list(candidate)}"
                )
            continue

        if header is None:
            continue

        if not in_table:
            continue

        rows.append(_row_to_mapping(cells, header, where=f"{current_code} 表格行"))

    flush()
    return blocks


# --------------------------------------------------------------------------- #
# 股票搜索（供客户端 UI 的搜索框使用）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StockMatch:
    """一条搜索命中。"""

    code: str
    name: str
    raw_code: str
    kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "rawCode": self.raw_code,
            "kind": self.kind,
        }


def parse_search_markdown(text: str) -> list[StockMatch]:
    """解析 ``westock search`` 的 Markdown 输出。

    真实输出形如::

        **股票** — 共 1 条，显示前 1

        | code | name | type |
        | --- | --- | --- |
        | sh600519 | 贵州茅台 | GP-A |

    无结果时只回一句"未找到匹配的结果（股票）。"——**这不是错误**，返回空列表。

    Raises:
        ProviderError: 命中限频提示（不能当成"没有这个股票"）。
        DataError: 有表格但表头不认识（字段错位比报错危险）。
    """
    stripped = text.strip()
    if not stripped:
        return []

    lowered = stripped.lower()
    for marker in RATE_LIMIT_MARKERS:
        if marker.lower() in lowered:
            raise ProviderError(
                f"westock search 返回了限频/服务端提示：{stripped.splitlines()[0]!r}"
            )

    if "未找到匹配" in stripped or "无匹配" in stripped:
        return []

    header: tuple[str, ...] | None = None
    matches: list[StockMatch] = []

    for line in stripped.splitlines():
        if not line.strip().startswith("|"):
            continue
        if _SEPARATOR_RE.match(line):
            continue
        cells = tuple(cell.strip() for cell in _split_row(line))

        if header is None:
            header = cells
            if header != SEARCH_HEADER:
                raise DataError(
                    "westock search 表头与预期不一致（字段可能已变更）：\n"
                    f"  期望：{list(SEARCH_HEADER)}\n"
                    f"  实际：{list(header)}"
                )
            continue

        row = _row_to_mapping(cells, header, where="search 行")
        raw_code = row.get("code", "").strip()
        name = row.get("name", "").strip()
        if not raw_code:
            continue
        try:
            code = normalize_code(raw_code)
        except Exception:
            # 港股/美股等非 A 股代码在当前账本里无法表示，跳过而不是报错
            continue
        matches.append(
            StockMatch(
                code=code,
                name=name or code,
                raw_code=raw_code,
                kind=row.get("type") or None,
            )
        )

    return matches


# --------------------------------------------------------------------------- #
# K 线：补齐 close_prev（除权除息日前收盘价）
# --------------------------------------------------------------------------- #
#
# 为什么需要：`dividend` 端点**不返回价格**，所以 `close_prev` 为 None，
# 股息率就是 null。要算股息率必须另取 K 线。
#
# 代价：每只股票多一次请求。限频很紧，所以这是在"股息率可用"与"请求数翻倍"
# 之间的取舍——因此做成可选开关，默认在联网取数时开启。


def parse_kline_markdown(text: str) -> dict[date, Decimal]:
    """解析 ``westock kline`` 的 Markdown 表格 → ``{交易日: 收盘价}``。

    真实表头：``| date | open | last | high | low | volume | amount | exchange | change_pct |``
    其中 ``last`` 是收盘价，``date`` 是 ``YYYY-MM-DD``。行按日期**降序**。

    Raises:
        ProviderError: 命中限频提示。
        DataError: 表头不符合预期（字段错位比报错危险）。
    """
    stripped = text.strip()
    if not stripped:
        raise ProviderError("westock kline 返回了空输出")
    lowered = stripped.lower()
    for marker in RATE_LIMIT_MARKERS:
        if marker.lower() in lowered:
            raise ProviderError(
                f"westock kline 返回了限频/服务端提示：{stripped.splitlines()[0]!r}"
            )

    header: tuple[str, ...] | None = None
    closes: dict[date, Decimal] = {}

    saw_separator = False

    for line in stripped.splitlines():
        if not line.strip().startswith("|"):
            continue
        if _SEPARATOR_RE.match(line):
            saw_separator = True
            continue
        cells = tuple(cell.strip() for cell in _split_row(line))

        if header is None:
            header = cells
            if header != KLINE_HEADER:
                # K 线的 date/last 是股息率的分母，认错了会算出错误的股息率。
                # 所以表头不符必须报错，不能静默返回空 map——那会让所有
                # close_prev 静默变成 None，看起来像"数据源没给价格"。
                raise DataError(
                    "westock K 线表头与预期不一致（字段可能已变更）：\n"
                    f"  期望：{list(KLINE_HEADER)}\n"
                    f"  实际：{list(header)}"
                )
            if not saw_separator:
                continue
            continue

        row = _row_to_mapping(cells, header, where="kline 行")
        raw_date = row.get("date", "")
        if len(raw_date) != 10:
            continue  # 只接受 YYYY-MM-DD；其它格式静默跳过（K 线不是账本数据）
        try:
            trading_day = date.fromisoformat(raw_date)
        except ValueError:
            continue
        try:
            close = Decimal(row.get("last", "").replace(",", ""))
        except (InvalidOperation, AttributeError):
            continue
        if close.is_finite():
            closes[trading_day] = close

    return closes


def enrich_close_prev(
    events: Sequence[DividendEvent],
    closes_by_code: Mapping[str, Mapping[date, Decimal]],
) -> tuple[list[DividendEvent], int]:
    """给事件补上 ``close_prev``：**除权除息日之前最近一个交易日**的收盘价。

    真实 K 线只含交易日，所以"前一交易日"就取日期严格小于 ``ex_date`` 的
    最大那一天——不需要交易日历。

    Returns:
        ``(补齐后的事件, 成功补齐条数)``。缺失价格的事件原样返回（``close_prev``
        保持 ``None``），**不猜**。
    """
    enriched: list[DividendEvent] = []
    filled = 0

    for event in events:
        closes = closes_by_code.get(event.code)
        if not closes:
            enriched.append(event)
            continue

        candidates = [day for day in closes if day < event.ex_date]
        if not candidates:
            enriched.append(event)
            continue

        close_prev = closes[max(candidates)]
        if close_prev <= ZERO:
            enriched.append(event)
            continue

        enriched.append(
            DividendEvent(
                code=event.code,
                ex_date=event.ex_date,
                dividend_per_share=event.dividend_per_share,
                date_basis=event.date_basis,
                pay_date=event.pay_date,
                per_share_bonus=event.per_share_bonus,
                per_share_transfer=event.per_share_transfer,
                right_reg_date=event.right_reg_date,
                close_prev=close_prev,
                plan_text=event.plan_text,
                proposal_sn=event.proposal_sn,
                status=event.status,
                source=event.source,
                source_ref={**dict(event.source_ref), "close_prev_from": "westock kline"},
            )
        )
        filled += 1

    return enriched, filled


# --------------------------------------------------------------------------- #
# 真实 CLI runner
# --------------------------------------------------------------------------- #


def make_cli_runner(
    *,
    binary: str = "westock",
    run: Callable[[Sequence[str]], Any] | None = None,
    sleep_seconds: float = 8.0,
    years: int = 10,
    request_all: bool = True,
    retries: int = 3,
    retry_backoff_seconds: float = 25.0,
    on_progress: Callable[[str], None] | None = None,
) -> Callable[[Sequence[str], date, date], dict[str, Any]]:
    """构造真正调用 ``westock`` CLI 的 runner。

    Args:
        binary: CLI 可执行文件名或路径。
        run: 注入用的执行函数（测试可传假的）。默认用 :mod:`subprocess` 真跑。
        sleep_seconds: 每只股票之间的间隔。实测连续请求会限频，
            8 秒间隔仍偶发失败，故默认 8 秒并配合下面的重试。
        years: ``--years`` 回溯年数。
        request_all: 是否带 ``--all``（含未实施方案，用于"待收分红"）。
        retries: 命中限频或拿到空输出时的重试次数。
        retry_backoff_seconds: 重试前的等待。限频按时间窗恢复，实测 20 秒以上才稳。
        on_progress: 进度回调（用于 CLI 提示，避免看起来像卡死）。

    Returns:
        ``runner(codes, start, end) -> {"text": <合并后的 Markdown>}``

    Raises:
        ProviderError: 重试用尽仍失败；或 CLI 不存在。
    """
    import subprocess
    import time

    def default_run(argv: Sequence[str]) -> tuple[str, str]:
        try:
            completed = subprocess.run(
                list(argv), capture_output=True, text=True, check=False
            )
        except FileNotFoundError:
            raise ProviderError(
                f"找不到 westock CLI（{argv[0]!r}）。请先安装："
                "bash docs/westock-skill-reference/scripts/setup.sh，"
                "或改用 --payload-file 离线解析，或 --from fixture"
            ) from None
        # 注意：CLI 在限频时退出码仍为 0，所以这里**不**检查 returncode，
        # 交给解析器识别提示文本。
        return completed.stdout, completed.stderr

    # executor 统一返回 (stdout, stderr)；注入的假 runner 可以只返回 stdout
    def call_executor(argv: Sequence[str]) -> tuple[str, str]:
        if run is None:
            return default_run(argv)
        result = run(argv)
        if isinstance(result, tuple):
            return result
        return str(result), ""

    #: 这些错误重试也没用（参数/代码格式问题），直接失败省时间
    permanent_markers = ("格式不支持", "unknown flag", "invalid argument", "invalid code")

    def fetch_one(canonical_code: str) -> str:
        # CLI 要的是前缀格式（sz000001），不是规范后缀格式（000001.SZ）
        cli_code = to_cli_code(canonical_code)
        argv = [binary, "dividend", cli_code, "--years", str(years)]
        if request_all:
            argv.append("--all")

        last_reason = ""
        for attempt in range(retries + 1):
            stdout, stderr = call_executor(argv)
            text = stdout.strip()
            diagnostic = stderr.strip().splitlines()[0].strip() if stderr.strip() else ""

            if text == "":
                # CLI 的失败信息在 stderr、退出码仍为 0，必须把它带出来
                # 否则错误消息只能是"空输出"，完全无法排查
                last_reason = (
                    f"CLI 无输出（stderr: {diagnostic}）" if diagnostic
                    else "CLI 返回了空输出（stderr 也为空）"
                )
            elif any(marker.lower() in text.lower() for marker in RATE_LIMIT_MARKERS):
                last_reason = text.splitlines()[0].strip()
            elif diagnostic and any(
                marker in diagnostic for marker in permanent_markers
            ):
                # 参数/代码格式类错误：重试无意义
                raise ProviderError(
                    f"取 {canonical_code} 失败：{diagnostic}"
                )
            else:
                return stdout

            if any(marker in last_reason for marker in permanent_markers):
                raise ProviderError(f"取 {canonical_code} 失败：{last_reason}")

            if attempt < retries:
                wait = retry_backoff_seconds * (attempt + 1)
                if on_progress is not None:
                    on_progress(
                        f"  {canonical_code}: {last_reason}，{wait:.0f} 秒后重试"
                        f"（{attempt + 2}/{retries + 1}）"
                    )
                if wait > 0:
                    time.sleep(wait)

        raise ProviderError(
            f"取 {canonical_code} 的分红数据失败（已重试 {retries} 次）：{last_reason}。"
            "限频按时间窗恢复；也可加大 --sleep 或减少同时查询的股票数。"
        )

    def runner(codes: Sequence[str], start: date, end: date) -> dict[str, Any]:
        chunks: list[str] = []
        for index, code in enumerate(codes):
            if index > 0 and sleep_seconds > 0:
                time.sleep(sleep_seconds)
            if on_progress is not None:
                on_progress(f"  取 {code}（{index + 1}/{len(codes)}）")
            chunks.append(fetch_one(code))
        return {"text": "\n\n".join(chunks), "codes": list(codes)}

    return runner


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #


class WestockProvider(DividendProvider):
    """走 ``westock`` CLI 的分红数据源。

    真实 CLI 通过 :func:`make_cli_runner` 注入；也可以注入假 runner 做离线测试。
    未注入 runner 时明确报错，**不会偷偷联网**。
    """

    name = "westock"
    shares_basis = 10

    def __init__(
        self,
        runner: Callable[[Sequence[str], date, date], Any] | None = None,
        *,
        years: int = 10,
        today: date | None = None,
    ) -> None:
        self._runner = runner
        self.years = years
        self._today = today

    def list_events(
        self, codes: Sequence[str], start: date, end: date
    ) -> ProviderResult:
        if self._runner is None:
            raise ProviderError(
                "westock provider 未注入 runner：请用 make_cli_runner() 构造真实 runner，"
                "或在测试中注入假 runner。本 provider 不会自行联网。"
            )
        payload = self._runner(list(codes), start, end)
        text = _extract_text(payload)
        result = parse_dividend_markdown(
            text,
            today=self._today,
            start=start,
            end=end,
            provenance={
                "requested_codes": list(codes),
                "window": [start.isoformat(), end.isoformat()],
                "years": self.years,
            },
        )
        for event in result.events:
            self._assert_per_share_scale(event)
        return result


def make_kline_runner(
    *,
    binary: str = "westock",
    run: Callable[[Sequence[str]], Any] | None = None,
    sleep_seconds: float = 8.0,
    retries: int = 3,
    retry_backoff_seconds: float = 25.0,
    on_progress: Callable[[str], None] | None = None,
) -> Callable[[Sequence[str], date, date], dict[str, Any]]:
    """构造取 K 线的 runner，返回 ``{代码: {交易日: 收盘价}}``。

    与 :func:`make_cli_runner` 同样处理限频、空输出与重试，
    并且**只有 ``date``/``last`` 是稳定字段**，其余列不参与解析。
    """
    import subprocess
    import time

    def default_run(argv: Sequence[str]) -> tuple[str, str]:
        try:
            completed = subprocess.run(
                list(argv), capture_output=True, text=True, check=False
            )
        except FileNotFoundError:
            raise ProviderError(
                f"找不到 westock CLI（{argv[0]!r}）；无法补齐除权前收盘价"
            ) from None
        return completed.stdout, completed.stderr

    def call_executor(argv: Sequence[str]) -> tuple[str, str]:
        if run is None:
            return default_run(argv)
        result = run(argv)
        if isinstance(result, tuple):
            return result
        return str(result), ""

    def runner(
        codes: Sequence[str], start: date, end: date
    ) -> dict[str, dict[date, Decimal]]:
        out: dict[str, dict[date, Decimal]] = {}
        for index, code in enumerate(codes):
            if index > 0 and sleep_seconds > 0:
                time.sleep(sleep_seconds)
            if on_progress is not None:
                on_progress(f"  取 {code} 的 K 线（{index + 1}/{len(codes)}）")

            argv = [
                binary,
                "kline",
                to_cli_code(code),
                "--period",
                "day",
                "--start",
                start.isoformat(),
                "--end",
                end.isoformat(),
            ]

            last_reason = ""
            for attempt in range(retries + 1):
                stdout, stderr = call_executor(argv)
                text = stdout.strip()
                diagnostic = stderr.strip().splitlines()[0].strip() if stderr.strip() else ""

                if text == "":
                    last_reason = (
                        f"CLI 无输出（stderr: {diagnostic}）" if diagnostic
                        else "CLI 返回了空输出"
                    )
                elif any(
                    marker.lower() in text.lower() for marker in RATE_LIMIT_MARKERS
                ):
                    last_reason = text.splitlines()[0].strip()
                else:
                    out[code] = parse_kline_markdown(text)
                    break

                if attempt < retries:
                    wait = retry_backoff_seconds * (attempt + 1)
                    if on_progress is not None:
                        on_progress(
                            f"  {code} K 线: {last_reason}，{wait:.0f} 秒后重试"
                            f"（{attempt + 2}/{retries + 1}）"
                        )
                    if wait > 0:
                        time.sleep(wait)
            else:
                # K 线只是"锦上添花"（决定股息率能不能算），取不到就不补，
                # 不该因此让整次同步失败
                if on_progress is not None:
                    on_progress(
                        f"  {code} K 线取数失败（{last_reason}）；"
                        "该股股息率将为空，其余数字不受影响"
                    )
        return out

    return runner


def make_price_runner(
    *,
    binary: str = "westock",
    run: Callable[[Sequence[str]], Any] | None = None,
    retries: int = 2,
    retry_backoff_seconds: float = 20.0,
) -> Callable[[Sequence[str]], dict[str, Decimal]]:
    """构造取价 runner：``codes -> {代码: 最新收盘价}``。

    为什么用 K 线取价：实测 westock **没有**个股行情快照命令（顶层只有 `kline`），
    所以取 `--limit 1` 拿到最新一根日线的收盘价。只取一条，不拉历史。

    注意：最新收盘价是**上一交易日**的收盘，不是盘中实时价。UI 上必须如实标注，
    不能说成"实时"。
    """
    import subprocess
    import time

    def default_run(argv: Sequence[str]) -> tuple[str, str]:
        try:
            completed = subprocess.run(
                list(argv), capture_output=True, text=True, check=False
            )
        except FileNotFoundError:
            raise ProviderError(f"找不到 westock CLI（{argv[0]!r}），无法取价") from None
        return completed.stdout, completed.stderr

    def call(argv: Sequence[str]) -> tuple[str, str]:
        if run is None:
            return default_run(argv)
        result = run(argv)
        if isinstance(result, tuple):
            return result
        return str(result), ""

    def one(code: str) -> Decimal | None:
        argv = [binary, "kline", to_cli_code(code), "--period", "day", "--limit", "1"]
        last_reason = ""
        for attempt in range(retries + 1):
            stdout, stderr = call(argv)
            text = stdout.strip()
            diagnostic = stderr.strip().splitlines()[0].strip() if stderr.strip() else ""
            if text == "" and diagnostic:
                last_reason = f"CLI 无输出（stderr: {diagnostic}）"
            elif any(marker.lower() in text.lower() for marker in RATE_LIMIT_MARKERS):
                last_reason = text.splitlines()[0].strip()
            else:
                closes = parse_kline_markdown(text)
                if not closes:
                    return None
                return closes[max(closes)]
            if attempt < retries:
                wait = retry_backoff_seconds * (attempt + 1)
                if wait > 0:
                    time.sleep(wait)
        raise ProviderError(f"取 {code} 的最新价失败（已重试 {retries} 次）：{last_reason}")

    def runner(codes: Sequence[str]) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for index, code in enumerate(codes):
            if index > 0:
                time.sleep(8)
            price = one(code)
            if price is not None and price > ZERO:
                out[code] = price
        return out

    return runner


def make_search_runner(
    *,
    binary: str = "westock",
    run: Callable[[Sequence[str]], Any] | None = None,
    retries: int = 2,
    retry_backoff_seconds: float = 20.0,
) -> Callable[[str], list[StockMatch]]:
    """构造搜索 runner：``query -> [StockMatch, ...]``。

    与其它 runner 一致地处理限频与空输出；搜索是交互式操作，所以默认重试 2 次。
    """
    import subprocess
    import time

    def default_run(argv: Sequence[str]) -> tuple[str, str]:
        try:
            completed = subprocess.run(
                list(argv), capture_output=True, text=True, check=False
            )
        except FileNotFoundError:
            raise ProviderError(f"找不到 westock CLI（{argv[0]!r}），无法搜索股票") from None
        return completed.stdout, completed.stderr

    def call(argv: Sequence[str]) -> tuple[str, str]:
        if run is None:
            return default_run(argv)
        result = run(argv)
        if isinstance(result, tuple):
            return result
        return str(result), ""

    def runner(query: str) -> list[StockMatch]:
        argv = [binary, "search", query]
        last_reason = ""
        for attempt in range(retries + 1):
            stdout, stderr = call(argv)
            text = stdout.strip()
            diagnostic = stderr.strip().splitlines()[0].strip() if stderr.strip() else ""

            if text == "" and diagnostic:
                last_reason = f"CLI 无输出（stderr: {diagnostic}）"
            elif any(marker.lower() in text.lower() for marker in RATE_LIMIT_MARKERS):
                last_reason = text.splitlines()[0].strip()
            else:
                return parse_search_markdown(text)

            if attempt < retries:
                wait = retry_backoff_seconds * (attempt + 1)
                if wait > 0:
                    time.sleep(wait)

        raise ProviderError(f"搜索 {query!r} 失败（已重试 {retries} 次）：{last_reason}")

    return runner


def _extract_text(payload: Any) -> str:
    """runner 可以返回字符串，或 ``{"text": ...}`` 信封。"""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, Mapping):
        for key in ("text", "stdout", "markdown"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    raise DataError(
        f"westock runner 的返回既不是字符串也不是含 text 的对象：{type(payload).__name__}"
    )
