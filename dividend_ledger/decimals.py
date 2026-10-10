"""Decimal 解析、精度与舍入策略（SPEC §4.5）。

三条铁律：

1. **解析只走字符串。** 传 ``float`` 进来一律拒绝——``0.1 + 0.2 != 0.3`` 这类误差
   一旦进入账本就再也追不回来。JSON 里的数字也先按字符串读取。
2. **舍入只在输出边界发生。** 中间过程保留完整精度，汇总只做 ``Decimal`` 加法，
   不做逐步舍入；只有转成对外字符串时才量化。
3. **JSON 中不出现浮点。** 所有金额/比例字段都是十进制字符串。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, getcontext
from typing import Iterable, Sequence

from dividend_ledger.errors import DecimalError

__all__ = [
    "MONEY_EXPONENT",
    "PER_SHARE_EXPONENT",
    "RATIO_EXPONENT",
    "ZERO",
    "add_all",
    "divide_ratio",
    "format_money",
    "format_per_share",
    "format_price",
    "format_ratio",
    "format_shares",
    "parse_decimal",
    "parse_int",
    "quantize_money",
    "quantize_per_share",
    "quantize_ratio",
]

# 每股派息/送转比例保留 6 位小数（SPEC §4.5）
PER_SHARE_EXPONENT = Decimal("0.000001")
# 金额保留 2 位小数（SPEC §4.5）
MONEY_EXPONENT = Decimal("0.01")
# 比率保留 6 位小数，输出为小数而非百分数
RATIO_EXPONENT = Decimal("0.000001")

ZERO = Decimal("0")

# 汇总可能有几千个事件，默认 28 位有效数字足够；显式声明以免受环境影响。
getcontext().prec = 50

_ROUNDING = ROUND_HALF_EVEN


def parse_decimal(value: object, *, field: str) -> Decimal:
    """把输入解析成 :class:`Decimal`。

    Args:
        value: 字符串、整数或 :class:`Decimal`。**不接受** ``float``。
        field: 字段名，仅用于报错信息。

    Raises:
        DecimalError: 类型不受支持、格式非法，或值不是有限数。
    """
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        # bool 是 int 的子类，必须先拦，否则 True 会变成 1
        raise DecimalError(f"{field} 不接受布尔值：{value!r}")
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise DecimalError(f"{field} 为空字符串，期望十进制数字")
        try:
            result = Decimal(text)
        except InvalidOperation:
            raise DecimalError(f"{field} 不是合法十进制数字：{value!r}") from None
    elif isinstance(value, float):
        raise DecimalError(
            f"{field} 收到浮点数 {value!r}；为避免精度误差，请改用字符串（如 \"{value!r}\"）"
        )
    else:
        raise DecimalError(f"{field} 类型不受支持：{type(value).__name__}（值 {value!r}）")

    if not result.is_finite():
        raise DecimalError(f"{field} 必须是有限数，收到 {value!r}")
    return result


def parse_int(value: object, *, field: str) -> int:
    """解析非负整数字段（如年份、条数）。

    Raises:
        DecimalError: 非法或为负。
    """
    number = parse_decimal(value, field=field)
    if number != number.to_integral_value():
        raise DecimalError(f"{field} 必须是整数，收到 {value!r}")
    result = int(number)
    if result < 0:
        raise DecimalError(f"{field} 不能为负数，收到 {value!r}")
    return result


def quantize_per_share(value: Decimal) -> Decimal:
    """每股派息/送转比例量化到 6 位小数。"""
    return value.quantize(PER_SHARE_EXPONENT, rounding=_ROUNDING)


def quantize_money(value: Decimal) -> Decimal:
    """金额量化到 2 位小数。"""
    return value.quantize(MONEY_EXPONENT, rounding=_ROUNDING)


def quantize_ratio(value: Decimal) -> Decimal:
    """比率量化到 6 位小数。"""
    return value.quantize(RATIO_EXPONENT, rounding=_ROUNDING)


def _zero_with_exponent(value: Decimal, exponent: Decimal) -> Decimal:
    """把零归一成"无符号 + 指定位数"的形式，顺手消掉 ``-0``。

    ``Decimal("-0").quantize(Decimal("0.01"))`` 会得到 ``-0.00``；
    ``Decimal("0.000000")`` 直接格式化会得到 ``0.000000`` 而不是 ``0``。
    账本里 ``-0.00`` 和 ``0.000000`` 都是噪声，统一在这里抹平。
    """
    digits = abs(exponent.as_tuple().exponent)
    return Decimal(0).quantize(Decimal(1).scaleb(-digits))


def _plain(value: Decimal) -> str:
    """输出普通十进制字符串，不用科学计数法。

    ``Decimal("0.000001").quantize(...)`` 在某些量级下会打印成 ``1E-6``，
    对下游解析者不友好，这里统一展开。
    """
    if value == 0:
        # 归一化 -0 与 0.00 等写法，保证逐字节稳定
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int) and exponent < 0:
            return "0." + "0" * (-exponent)
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".") or "0"
    return text


def format_per_share(value: Decimal) -> str:
    """每股派息/送转比例的对外字符串。

    **恒保留 6 位小数**，不做 trim。理由：这些值是"精度敏感"的输入
    （0.1 与 0.100000 在账本里应当长得一样），统一形态才能让黄金基线与
    ``diff`` 对比保持稳定；人读的渲染层负责去零（见 :mod:`dividend_ledger.render`）。
    """
    quantized = quantize_per_share(value)
    if quantized == 0:
        quantized = _zero_with_exponent(quantized, PER_SHARE_EXPONENT)
    return f"{quantized:f}"


def format_price(value: Decimal) -> str:
    """价格（每股成本、除权前收盘价）的对外字符串：恒保留 2 位小数。

    价格与"每股派息"精度需求不同：成交价最小变动单位是 0.01 元，
    所以用金额口径而非 6 位口径，避免出现 ``380.000000`` 这种噪声。
    """
    quantized = quantize_money(value)
    if quantized == 0:
        quantized = _zero_with_exponent(quantized, MONEY_EXPONENT)
    return f"{quantized:f}"


def format_shares(value: Decimal) -> str:
    """股数的对外字符串：去掉无意义的小数尾零（``40.000000`` → ``40``）。"""
    if value == 0:
        return "0"
    return _plain(value)


def format_ratio(value: Decimal) -> str:
    """比率的对外字符串，恒保留 6 位小数，输出小数而非百分数。"""
    quantized = quantize_ratio(value)
    if quantized == 0:
        quantized = _zero_with_exponent(quantized, RATIO_EXPONENT)
    return f"{quantized:f}"


def format_money(value: Decimal, *, trim: bool = False) -> str:
    """金额的对外字符串（2 位小数）。

    Args:
        trim: 默认 ``False``，即恒保留 2 位（``"1200.00"``），让账本读起来一致。
    """
    quantized = quantize_money(value)
    if quantized == 0:
        quantized = _zero_with_exponent(quantized, MONEY_EXPONENT)
    return _plain(quantized) if trim else f"{quantized:f}"


def add_all(values: Iterable[Decimal]) -> Decimal:
    """精确求和，不做中间舍入。

    用 ``sum`` 从 :data:`ZERO` 起累加即可——``Decimal`` 加法本身是精确的
    （受上下文精度限制，已显式放宽到 50 位有效数字）。
    """
    total = ZERO
    for value in values:
        total += value
    return total


def divide_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    """计算比率。

    Raises:
        DecimalError: 分母为 0——账本里这意味着"没有成本却要算收益率"，
            调用方应据此返回 ``None`` 而不是 0（SPEC §4.4 禁止用 0 冒充缺失）。
    """
    if denominator == 0:
        raise DecimalError("比率分母为 0，无法计算")
    return numerator / denominator


def to_json_scalar(value: Decimal) -> str:
    """把 Decimal 转成 JSON 中可安全序列化的十进制字符串。"""
    return _plain(value)


def normalize_for_output(values: Sequence[Decimal]) -> list[str]:
    """批量转成十进制字符串（金额口径）。"""
    return [format_money(value) for value in values]
