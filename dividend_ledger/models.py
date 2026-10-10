"""数据模型（SPEC §3）。

三个模型都是**不可变**的（``frozen=True, slots=True``）：账本里改一条记录必须是显式新建，
不允许就地修改，避免汇总过程中数据被悄悄改动。

所有 ``from_dict`` 都做**严格校验**：字段名拼错、类型不对、日期格式错都会立刻报错，
不做静默忽略——账本最怕的就是"少读了一条但没人知道"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar, Mapping

from dividend_ledger.codes import normalize_code
from dividend_ledger.decimals import (
    ZERO,
    format_money,
    format_per_share,
    format_price,
    format_shares,
    parse_decimal,
)
from dividend_ledger.errors import DataError, DecimalError

__all__ = [
    "DATE_FORMAT",
    "DateBasis",
    "DividendEvent",
    "DividendStatus",
    "Holding",
    "LedgerEntry",
    "parse_date",
]

DATE_FORMAT = "YYYY-MM-DD"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 区分"字段未出现"与"字段显式为 null"
_MISSING = object()


def parse_date(value: object, *, field_name: str) -> date:
    """严格解析 ``YYYY-MM-DD`` 日期。

    Raises:
        DataError: 非字符串、格式不符，或不是真实存在的日期（如 2024-02-30）。
    """
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise DataError(
            f"{field_name} 必须是 {DATE_FORMAT} 字符串，收到 {type(value).__name__}: {value!r}"
        )
    text = value.strip()
    if not _DATE_RE.match(text):
        raise DataError(f"{field_name} 必须是 {DATE_FORMAT} 格式，收到 {value!r}")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise DataError(f"{field_name} 不是合法日期：{value!r}") from None


def _require_mapping(payload: object, *, where: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise DataError(f"{where} 必须是对象（JSON object），收到 {type(payload).__name__}")
    return payload


def _reject_unknown(
    payload: Mapping[str, Any], *, known: frozenset[str], where: str
) -> None:
    unknown = sorted(set(payload) - known)
    if unknown:
        raise DataError(f"{where} 含未知字段 {unknown}；已知字段：{sorted(known)}")


def _optional_str(payload: Mapping[str, Any], key: str, *, where: str) -> str | None:
    value = payload.get(key, _MISSING)
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, str):
        raise DataError(f"{where}.{key} 必须是字符串，收到 {type(value).__name__}: {value!r}")
    return value.strip() or None


def _normalize_code_field(payload: Mapping[str, Any], *, where: str) -> str:
    try:
        return normalize_code(payload["code"])
    except DataError:
        raise
    except Exception as exc:  # CodeError 已带可读消息
        raise DataError(f"{where}.code 非法：{exc}") from None


def _optional_decimal(
    payload: Mapping[str, Any],
    key: str,
    *,
    where: str,
    non_negative: bool = True,
) -> Decimal | None:
    raw = payload.get(key, _MISSING)
    if raw is _MISSING or raw is None:
        return None
    try:
        value = parse_decimal(raw, field=f"{where}.{key}")
    except DecimalError as exc:
        raise DataError(str(exc)) from None
    if non_negative and value < ZERO:
        raise DataError(f"{where}.{key} 不能为负数，收到 {raw!r}")
    return value


def _required_decimal(value: object, *, where: str) -> Decimal:
    """解析必填十进制字段，把 :class:`DecimalError` 统一包成 :class:`DataError`。"""
    try:
        return parse_decimal(value, field=where)
    except DecimalError as exc:
        raise DataError(str(exc)) from None


def _optional_source_ref(payload: Mapping[str, Any], *, where: str) -> Mapping[str, Any]:
    raw = payload.get("source_ref", _MISSING)
    if raw is _MISSING or raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise DataError(f"{where}.source_ref 必须是对象，收到 {type(raw).__name__}")
    return dict(raw)


def _parse_status(
    payload: Mapping[str, Any], *, ex_date: date, where: str
) -> "DividendStatus":
    """解析方案状态。

    显式给了 ``status`` 就用它；没给则按 ``ex_date`` 与今天的关系推断——
    这是为了让一阶段的 fixture（没有 ``status`` 字段）继续按"已实施"处理，
    同时真实数据里"方案实施 + 未来除权日"能自动落到 ``CONFIRMED``。
    """
    raw = payload.get("status", _MISSING)
    if raw is _MISSING or raw is None:
        return (
            DividendStatus.IMPLEMENTED
            if ex_date <= date.today()
            else DividendStatus.CONFIRMED
        )
    try:
        return DividendStatus(raw)
    except ValueError:
        allowed = [member.value for member in DividendStatus]
        raise DataError(f"{where}.status 非法：{raw!r}；允许值 {allowed}") from None


class DateBasis(str, Enum):
    """该条记录归集日期所依据的来源（SPEC §3.2 / §4.1）。"""

    #: 数据源只给了除权除息日，用它归集
    EX_DATE = "ex_date"
    #: 数据源声明了派息到账日
    PAY_DATE_DECLARED = "pay_date_declared"
    #: 命中 ``data/pay_dates.json`` 人工覆盖表
    PAY_DATE_OVERRIDE = "pay_date_override"


class DividendStatus(str, Enum):
    """分红方案的实施状态（SPEC §4.7）。

    实测（见 docs/westock-verified-behaviour.md）：公开数据里 `方案实施` 的行
    也**可能带未来的除权除息日**，所以状态**不能只看文本**，必须结合日期判断。
    """

    #: 除权除息日已过 → 钱（应该）已经到账
    IMPLEMENTED = "implemented"
    #: 方案已实施公告，但除权除息日还没到 → 这就是"未来预期分红"的来源
    CONFIRMED = "confirmed"
    #: 只有董事会预案，还没定实施日 → 可能被否/被改
    PROPOSED = "proposed"


@dataclass(frozen=True, slots=True)
class Holding:
    """一条持仓（SPEC §3.1）。

    Attributes:
        code: 规范代码 ``600519.SH``。
        start_date: 开始持有日。
        shares: 持有股数（持有期内恒定——一阶段不建模加仓/减仓）。
        cost_per_share: 每股成本；``None`` 表示未提供，该股成本收益率将是 ``None``。
        name: 证券名称，仅展示用。
        account: 账户标签，预留多账户。
    """

    code: str
    start_date: date
    shares: Decimal
    cost_per_share: Decimal | None = None
    name: str | None = None
    account: str | None = None

    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"code", "start_date", "shares", "cost_per_share", "name", "account"}
    )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, where: str = "holding") -> "Holding":
        payload = _require_mapping(payload, where=where)
        _reject_unknown(payload, known=cls.FIELDS, where=where)

        for required in ("code", "start_date", "shares"):
            if required not in payload:
                raise DataError(f"{where} 缺少必填字段 {required}")

        try:
            shares = parse_decimal(payload["shares"], field=f"{where}.shares")
        except DecimalError as exc:
            raise DataError(str(exc)) from None
        if shares < ZERO:
            raise DataError(f"{where}.shares 不能为负数，收到 {payload['shares']!r}")

        return cls(
            code=_normalize_code_field(payload, where=where),
            start_date=parse_date(payload["start_date"], field_name=f"{where}.start_date"),
            shares=shares,
            cost_per_share=_optional_decimal(payload, "cost_per_share", where=where),
            name=_optional_str(payload, "name", where=where),
            account=_optional_str(payload, "account", where=where),
        )

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典（数值一律十进制字符串）。"""
        result: dict[str, Any] = {
            "code": self.code,
            "start_date": self.start_date.isoformat(),
            "shares": format_shares(self.shares),
        }
        if self.cost_per_share is not None:
            result["cost_per_share"] = format_price(self.cost_per_share)
        if self.name is not None:
            result["name"] = self.name
        if self.account is not None:
            result["account"] = self.account
        return result

    @property
    def cost_basis(self) -> Decimal | None:
        """持有成本 = 股数 × 每股成本；未提供成本时为 ``None``。"""
        if self.cost_per_share is None:
            return None
        return self.shares * self.cost_per_share


@dataclass(frozen=True, slots=True)
class DividendEvent:
    """一条分红事件（SPEC §3.2）。

    ``dividend_per_share`` 一律是**每股**税前金额；源数据是"每 10 股"口径时，
    必须在 provider 层 ÷10 后落库（westock 的 ``cashDiviRMB`` 就是这种情况）。
    """

    code: str
    ex_date: date
    dividend_per_share: Decimal
    date_basis: DateBasis = DateBasis.EX_DATE
    pay_date: date | None = None
    per_share_bonus: Decimal = ZERO
    per_share_transfer: Decimal = ZERO
    right_reg_date: date | None = None
    close_prev: Decimal | None = None
    plan_text: str | None = None
    #: 同一除权除息日可能有多条方案（普通分红 + 特殊分红），靠它区分
    proposal_sn: str | None = None
    status: DividendStatus = DividendStatus.IMPLEMENTED
    source: str = "fixture"
    source_ref: Mapping[str, Any] = field(default_factory=dict)

    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "code",
            "ex_date",
            "dividend_per_share",
            "date_basis",
            "pay_date",
            "per_share_bonus",
            "per_share_transfer",
            "right_reg_date",
            "close_prev",
            "plan_text",
            "proposal_sn",
            "status",
            "source",
            "source_ref",
        }
    )

    @property
    def natural_key(self) -> tuple[str, str, str]:
        """现金分红事件的业务唯一键：``(code, ex_date, proposal_sn)``。

        为什么必须带 ``proposal_sn``：实测发现同一除权除息日可以有**多条**方案
        （宁德时代 2026-04-22：普通分红 21.78 元 + 特殊分红 47.79 元）。
        只用 ``(code, ex_date)`` 会把它们合并，**直接丢掉一笔分红**。
        缺 ``proposal_sn`` 的数据（如一阶段的 fixture）退化为空串，行为不变。
        """
        return (self.code, self.ex_date.isoformat(), self.proposal_sn or "")

    @property
    def bonus_key(self) -> tuple[str, str, str, str]:
        """送转事件的业务唯一键：``(code, ex_date, proposal_sn, "bonus")``。"""
        return (self.code, self.ex_date.isoformat(), self.proposal_sn or "", "bonus")

    @property
    def is_future(self) -> bool:
        """是否尚未到除权除息日（即"未来预期分红"）。"""
        return self.status is not DividendStatus.IMPLEMENTED

    @property
    def has_bonus(self) -> bool:
        """是否含送股或转增。"""
        return self.per_share_bonus > ZERO or self.per_share_transfer > ZERO

    @property
    def effective_date(self) -> date:
        """本条记录实际参与归集的日期：有到账日用得到账日，否则用除权除息日。"""
        if self.date_basis is DateBasis.EX_DATE:
            return self.ex_date
        if self.pay_date is None:  # pragma: no cover - 构造期已保证
            raise DataError(
                f"{self.code}@{self.ex_date.isoformat()} 的 date_basis 为 "
                f"{self.date_basis.value} 但缺少 pay_date"
            )
        return self.pay_date

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any], *, where: str = "event"
    ) -> "DividendEvent":
        payload = _require_mapping(payload, where=where)
        _reject_unknown(payload, known=cls.FIELDS, where=where)

        for required in ("code", "ex_date", "dividend_per_share"):
            if required not in payload:
                raise DataError(f"{where} 缺少必填字段 {required}")

        raw_basis = payload.get("date_basis", DateBasis.EX_DATE.value)
        try:
            date_basis = DateBasis(raw_basis)
        except ValueError:
            allowed = [member.value for member in DateBasis]
            raise DataError(
                f"{where}.date_basis 非法：{raw_basis!r}；允许值 {allowed}"
            ) from None

        pay_raw = payload.get("pay_date", _MISSING)
        pay_date = (
            None
            if pay_raw is _MISSING or pay_raw is None
            else parse_date(pay_raw, field_name=f"{where}.pay_date")
        )

        # date_basis 与实际是否给了 pay_date 必须自洽，否则报告的归集依据会撒谎
        if date_basis is not DateBasis.EX_DATE and pay_date is None:
            raise DataError(
                f"{where}.date_basis 为 {date_basis.value} 但未提供 pay_date；"
                "两者必须一致，否则报告的归集依据会失真"
            )

        ex_date = parse_date(payload["ex_date"], field_name=f"{where}.ex_date")

        return cls(
            code=_normalize_code_field(payload, where=where),
            ex_date=ex_date,
            dividend_per_share=_required_decimal(
                payload["dividend_per_share"], where=f"{where}.dividend_per_share"
            ),
            date_basis=date_basis,
            pay_date=pay_date,
            per_share_bonus=_optional_decimal(payload, "per_share_bonus", where=where)
            or ZERO,
            per_share_transfer=_optional_decimal(
                payload, "per_share_transfer", where=where
            )
            or ZERO,
            right_reg_date=(
                None
                if payload.get("right_reg_date") is None
                else parse_date(
                    payload["right_reg_date"], field_name=f"{where}.right_reg_date"
                )
            ),
            close_prev=_optional_decimal(payload, "close_prev", where=where),
            plan_text=_optional_str(payload, "plan_text", where=where),
            proposal_sn=_optional_str(payload, "proposal_sn", where=where),
            status=_parse_status(payload, ex_date=ex_date, where=where),
            source=_optional_str(payload, "source", where=where) or "fixture",
            source_ref=_optional_source_ref(payload, where=where),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "ex_date": self.ex_date.isoformat(),
            "dividend_per_share": format_per_share(self.dividend_per_share),
            "date_basis": self.date_basis.value,
            "source": self.source,
        }
        if self.pay_date is not None:
            result["pay_date"] = self.pay_date.isoformat()
        if self.per_share_bonus != ZERO:
            result["per_share_bonus"] = format_per_share(self.per_share_bonus)
        if self.per_share_transfer != ZERO:
            result["per_share_transfer"] = format_per_share(self.per_share_transfer)
        if self.right_reg_date is not None:
            result["right_reg_date"] = self.right_reg_date.isoformat()
        if self.close_prev is not None:
            result["close_prev"] = format_price(self.close_prev)
        if self.plan_text is not None:
            result["plan_text"] = self.plan_text
        if self.proposal_sn is not None:
            result["proposal_sn"] = self.proposal_sn
        result["status"] = self.status.value
        if self.source_ref:
            result["source_ref"] = dict(self.source_ref)
        return result


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """一条计算明细（SPEC §3.3）——由插件派生，不是输入。

    所有比率字段在**输入不足**时为 ``None``，绝不用 0 冒充（SPEC §4.4）；
    ``missing_inputs`` 用可读标签说明是缺了哪个输入。
    """

    code: str
    name: str | None
    ex_date: date
    effective_date: date
    date_basis: DateBasis
    shares_at_event: Decimal
    dividend_per_share: Decimal
    cash_amount: Decimal
    bonus_shares: Decimal
    transfer_shares: Decimal
    cost_basis: Decimal | None
    yield_on_cost: Decimal | None
    dividend_yield: Decimal | None
    tax_reference: Decimal | None
    source: str
    #: 除权除息日前收盘价（保留原始输入，供加权股息率精确计算）
    close_prev: Decimal | None = None
    missing_inputs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "ex_date": self.ex_date.isoformat(),
            "effective_date": self.effective_date.isoformat(),
            "date_basis": self.date_basis.value,
            "shares_at_event": format_shares(self.shares_at_event),
            "dividend_per_share": format_per_share(self.dividend_per_share),
            "cash_amount": format_money(self.cash_amount),
            "bonus_shares": format_shares(self.bonus_shares),
            "transfer_shares": format_shares(self.transfer_shares),
            "cost_basis": None if self.cost_basis is None else format_money(self.cost_basis),
            "yield_on_cost": (
                None if self.yield_on_cost is None else format_per_share(self.yield_on_cost)
            ),
            "dividend_yield": (
                None
                if self.dividend_yield is None
                else format_per_share(self.dividend_yield)
            ),
            "tax_reference": (
                None if self.tax_reference is None else format_money(self.tax_reference)
            ),
            "source": self.source,
        }
        if self.close_prev is not None:
            result["close_prev"] = format_price(self.close_prev)
        if self.name is not None:
            result["name"] = self.name
        if self.missing_inputs:
            result["missing_inputs"] = list(self.missing_inputs)
        return result
