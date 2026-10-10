"""存储层测试：自然键幂等合并与导入审计（SPEC §4.6）。

核心命题：**分红事件是声明式快照，不是事件流。**
同一份输入导入两次结果必须完全相同；改一个数字后再导入要覆盖旧值，
而不是留下一条"幽灵记录"。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from dividend_ledger.errors import DataError, DecimalError
from dividend_ledger.models import DividendEvent, Holding
from dividend_ledger.store import (
    DataPaths,
    apply_pay_date_overrides,
    import_events,
    import_holdings,
    load_events,
    load_holdings,
    load_pay_date_overrides,
    save_events,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_EVENTS = REPO_ROOT / "data" / "events.fixture.json"
FIXTURE_HOLDINGS = REPO_ROOT / "data" / "holdings.json"


def make_event(**overrides) -> DividendEvent:
    base = {
        "code": "600519.SH",
        "ex_date": "2024-06-19",
        "dividend_per_share": "30.876",
    }
    base.update(overrides)
    return DividendEvent.from_dict(base)


class TempDataMixin:
    def setUp(self) -> None:  # noqa: D102
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = DataPaths(root=Path(self._tmp.name))
        self.paths.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:  # noqa: D102
        self._tmp.cleanup()


class EventIdempotencyTest(TempDataMixin, unittest.TestCase):
    def test_first_import_counts_as_imported(self) -> None:
        audit = import_events(self.paths.events, [make_event()])
        self.assertEqual(audit.imported, 1)
        self.assertEqual(audit.replaced, 0)
        self.assertEqual(audit.unchanged, 0)

    def test_second_identical_import_is_noop(self) -> None:
        """重复导入必须幂等。"""
        import_events(self.paths.events, [make_event()])
        first = self.paths.events.read_bytes()

        audit = import_events(self.paths.events, [make_event()])
        self.assertEqual(audit.unchanged, 1)
        self.assertEqual(audit.imported, 0)
        self.assertEqual(audit.replaced, 0)
        self.assertEqual(self.paths.events.read_bytes(), first)

    def test_changed_value_replaces_and_leaves_no_ghost(self) -> None:
        """改一个数字再导入 → 覆盖旧值，记录数不变。"""
        import_events(self.paths.events, [make_event(dividend_per_share="30.876")])
        audit = import_events(self.paths.events, [make_event(dividend_per_share="31.0")])

        self.assertEqual(audit.replaced, 1)
        self.assertEqual(audit.imported, 0)

        events, _ = load_events(self.paths.events)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].dividend_per_share, __import__("decimal").Decimal("31.0"))

    def test_bonus_key_merge(self) -> None:
        """同一事件先只有派现、后补上送股，应合并为一条而不是两条。"""
        import_events(self.paths.events, [make_event()])
        audit = import_events(
            self.paths.events, [make_event(per_share_bonus="0.1")]
        )
        self.assertEqual(audit.replaced, 1)
        events, _ = load_events(self.paths.events)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].per_share_bonus, __import__("decimal").Decimal("0.1"))

    def test_different_ex_date_is_a_new_record(self) -> None:
        import_events(self.paths.events, [make_event()])
        audit = import_events(self.paths.events, [make_event(ex_date="2025-06-20")])
        self.assertEqual(audit.imported, 1)
        events, _ = load_events(self.paths.events)
        self.assertEqual(len(events), 2)

    def test_different_code_same_date_is_a_new_record(self) -> None:
        import_events(self.paths.events, [make_event()])
        audit = import_events(self.paths.events, [make_event(code="000001.SZ")])
        self.assertEqual(audit.imported, 1)

    def test_whole_fixture_is_idempotent(self) -> None:
        """用真实 fixture 全量跑两遍，逐字节一致。"""
        raw = json.loads(FIXTURE_EVENTS.read_text(encoding="utf-8"))
        events = [
            DividendEvent.from_dict(item, where=f"e[{i}]")
            for i, item in enumerate(raw["events"])
        ]
        import_events(self.paths.events, events)
        first = self.paths.events.read_bytes()
        audit = import_events(self.paths.events, events)

        self.assertEqual(audit.unchanged, len(events))
        self.assertEqual(audit.imported, 0)
        self.assertEqual(audit.replaced, 0)
        self.assertEqual(self.paths.events.read_bytes(), first)

    def test_mixed_new_and_unchanged(self) -> None:
        import_events(self.paths.events, [make_event()])
        audit = import_events(
            self.paths.events,
            [make_event(), make_event(ex_date="2025-06-20")],
        )
        self.assertEqual(audit.unchanged, 1)
        self.assertEqual(audit.imported, 1)

    def test_corrupt_existing_file_blocks_merge(self) -> None:
        """已有坏行时拒绝合并——否则重写文件会把它们静默丢掉。"""
        self.paths.events.write_text(
            json.dumps({"events": [{"code": "bad", "ex_date": "x"}]}),
            encoding="utf-8",
        )
        with self.assertRaises(DataError) as ctx:
            import_events(self.paths.events, [make_event()])
        self.assertIn("静默丢弃", str(ctx.exception))


class EventPersistenceTest(TempDataMixin, unittest.TestCase):
    def test_sorted_output_is_stable(self) -> None:
        events = [
            make_event(code="000001.SZ", ex_date="2024-06-07"),
            make_event(code="600519.SH", ex_date="2024-06-19"),
            make_event(code="600519.SH", ex_date="2023-06-16"),
        ]
        save_events(self.paths.events, events)
        loaded, _ = load_events(self.paths.events)
        self.assertEqual(
            [(e.code, e.ex_date) for e in loaded],
            [
                ("000001.SZ", date(2024, 6, 7)),
                ("600519.SH", date(2023, 6, 16)),
                ("600519.SH", date(2024, 6, 19)),
            ],
        )

    def test_saving_same_content_twice_is_byte_identical(self) -> None:
        events = [make_event(), make_event(ex_date="2023-06-16")]
        save_events(self.paths.events, events)
        first = self.paths.events.read_bytes()
        save_events(self.paths.events, list(reversed(events)))
        self.assertEqual(self.paths.events.read_bytes(), first)

    def test_bare_array_file_accepted(self) -> None:
        self.paths.events.write_text(
            json.dumps(
                [
                    {
                        "code": "600519.SH",
                        "ex_date": "2024-06-19",
                        "dividend_per_share": "1",
                    }
                ]
            ),
            encoding="utf-8",
        )
        events, rejected = load_events(self.paths.events)
        self.assertEqual(len(events), 1)
        self.assertEqual(rejected, [])

    def test_missing_file_is_optional(self) -> None:
        events, rejected = load_events(self.paths.events, required=False)
        self.assertEqual(events, [])
        self.assertEqual(rejected, [])

    def test_missing_required_file_raises(self) -> None:
        with self.assertRaises(DataError):
            load_events(self.paths.events, required=True)

    def test_bad_rows_are_reported_with_index(self) -> None:
        self.paths.events.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "code": "600519.SH",
                            "ex_date": "2024-06-19",
                            "dividend_per_share": "1",
                        },
                        {"code": "600519.SH", "ex_date": "nope", "dividend_per_share": "1"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        events, rejected = load_events(self.paths.events)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].index, 1)

    def test_empty_file_raises_readable_error(self) -> None:
        self.paths.events.write_text("   ", encoding="utf-8")
        with self.assertRaises(DataError) as ctx:
            load_events(self.paths.events)
        self.assertIn("为空", str(ctx.exception))

    def test_invalid_json_raises_with_line(self) -> None:
        self.paths.events.write_text('{"events": [}', encoding="utf-8")
        with self.assertRaises(DataError) as ctx:
            load_events(self.paths.events)
        self.assertIn("行", str(ctx.exception))

    def test_json_output_has_no_floats(self) -> None:
        save_events(self.paths.events, [make_event(close_prev="1500")])
        text = self.paths.events.read_text(encoding="utf-8")
        payload = json.loads(text)
        entry = payload["events"][0]
        for key in ("dividend_per_share", "close_prev"):
            self.assertIsInstance(entry[key], str, key)


class HoldingsImportTest(TempDataMixin, unittest.TestCase):
    def test_import_and_replace_by_code(self) -> None:
        first = Holding.from_dict(
            {"code": "600519.SH", "start_date": "2021-03-15", "shares": "100"}
        )
        import_holdings(self.paths.holdings, [first])

        second = Holding.from_dict(
            {"code": "sh600519", "start_date": "2021-03-15", "shares": "200"}
        )
        audit = import_holdings(self.paths.holdings, [second])
        self.assertEqual(audit.replaced, 1)

        holdings, _ = load_holdings(self.paths.holdings)
        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0].shares, __import__("decimal").Decimal("200"))

    def test_real_fixture_loads(self) -> None:
        holdings, rejected = load_holdings(FIXTURE_HOLDINGS)
        self.assertEqual(rejected, [])
        self.assertEqual(len(holdings), 6)
        codes = [h.code for h in holdings]
        self.assertIn("600519.SH", codes)
        self.assertIn("601988.SH", codes)

    def test_fixture_covers_missing_input_scenarios(self) -> None:
        holdings, _ = load_holdings(FIXTURE_HOLDINGS)
        by_code = {h.code: h for h in holdings}
        self.assertIsNone(by_code["601988.SH"].cost_per_share, "应有一只无成本")
        self.assertIsNotNone(by_code["600519.SH"].cost_per_share)


class PayDateOverrideTest(TempDataMixin, unittest.TestCase):
    def test_mapping_form(self) -> None:
        self.paths.pay_dates.write_text(
            json.dumps({"600519.SH|2024-06-19": "2024-06-26"}), encoding="utf-8"
        )
        overrides, rejected = load_pay_date_overrides(self.paths.pay_dates)
        self.assertEqual(rejected, [])
        self.assertEqual(overrides[("600519.SH", "2024-06-19")], date(2024, 6, 26))

    def test_list_form(self) -> None:
        self.paths.pay_dates.write_text(
            json.dumps(
                {
                    "overrides": [
                        {
                            "code": "sh600519",
                            "ex_date": "2024-06-19",
                            "pay_date": "2024-06-26",
                            "note": "券商流水",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        overrides, rejected = load_pay_date_overrides(self.paths.pay_dates)
        self.assertEqual(rejected, [])
        self.assertEqual(overrides[("600519.SH", "2024-06-19")], date(2024, 6, 26))

    def test_at_separator_also_works(self) -> None:
        self.paths.pay_dates.write_text(
            json.dumps({"600519.SH@2024-06-19": "2024-06-26"}), encoding="utf-8"
        )
        overrides, _ = load_pay_date_overrides(self.paths.pay_dates)
        self.assertIn(("600519.SH", "2024-06-19"), overrides)

    def test_missing_file_is_fine(self) -> None:
        overrides, rejected = load_pay_date_overrides(self.paths.pay_dates)
        self.assertEqual(overrides, {})
        self.assertEqual(rejected, [])

    def test_apply_sets_basis_and_pay_date(self) -> None:
        event = make_event()
        applied, stats = apply_pay_date_overrides(
            [event], {("600519.SH", "2024-06-19"): date(2024, 6, 26)}
        )
        self.assertEqual(stats["overrides_applied"], 1)
        self.assertEqual(applied[0].date_basis.value, "pay_date_override")
        self.assertEqual(applied[0].pay_date, date(2024, 6, 26))
        self.assertEqual(applied[0].effective_date, date(2024, 6, 26))

    def test_unmatched_override_is_reported(self) -> None:
        """僵尸覆盖必须暴露出来，否则用户以为生效了。"""
        _, stats = apply_pay_date_overrides(
            [make_event()], {("000001.SZ", "2024-06-07"): date(2024, 6, 14)}
        )
        self.assertEqual(stats["overrides_applied"], 0)
        self.assertEqual(stats["overrides_unmatched"], ["000001.SZ|2024-06-07"])

    def test_override_does_not_mutate_original(self) -> None:
        event = make_event()
        apply_pay_date_overrides([event], {("600519.SH", "2024-06-19"): date(2024, 6, 26)})
        self.assertIsNone(event.pay_date)
        self.assertEqual(event.date_basis.value, "ex_date")

    def test_bad_key_is_rejected_not_crashed(self) -> None:
        self.paths.pay_dates.write_text(
            json.dumps({"no-separator": "2024-06-26"}), encoding="utf-8"
        )
        overrides, rejected = load_pay_date_overrides(self.paths.pay_dates)
        self.assertEqual(overrides, {})
        self.assertEqual(len(rejected), 1)


class ErrorContractTest(unittest.TestCase):
    def test_decimal_error_is_value_error(self) -> None:
        self.assertTrue(issubclass(DecimalError, ValueError))

    def test_all_errors_share_base(self) -> None:
        from dividend_ledger.errors import CodeError, DividendLedgerError, ProviderError

        for cls in (CodeError, DecimalError, DataError, ProviderError):
            self.assertTrue(issubclass(cls, DividendLedgerError), cls)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
