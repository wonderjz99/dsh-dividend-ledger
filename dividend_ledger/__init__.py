"""A 股分红账本 —— 离线核心。

设计约束（见 SPEC.md）：
- Python 3.11+，**零第三方依赖**（只用标准库）。
- 金额与比例一律用 :class:`decimal.Decimal`，对外输出十进制字符串，JSON 中不出现浮点。
- 业务计算全部在本包内；provider 只负责把外部数据翻译成规范字段。

模块职责：
- :mod:`dividend_ledger.codes`     证券代码归一化
- :mod:`dividend_ledger.decimals`  Decimal 解析、精度与舍入
- :mod:`dividend_ledger.models`    数据模型
- :mod:`dividend_ledger.store`     data/ 读写与自然键幂等合并
- :mod:`dividend_ledger.providers` 分红事件来源
- :mod:`dividend_ledger.ledger`    归属匹配与单事件计算
- :mod:`dividend_ledger.report`    三分区汇总
- :mod:`dividend_ledger.cli`       命令行入口
"""

from dividend_ledger.errors import (
    DataError,
    DividendLedgerError,
    ProviderError,
)

__all__ = [
    "DataError",
    "DividendLedgerError",
    "ProviderError",
]

__version__ = "0.1.0"
