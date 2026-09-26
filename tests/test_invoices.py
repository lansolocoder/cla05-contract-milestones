"""开票、收款、发票作废与账期风险查询的端到端 CLI 测试。

每个用例使用独立临时数据库（CONTRACT_LEDGER_DB），与其他测试隔离。
金额一律按元的数字比较，日期按 YYYY-MM-DD 文本比较。
query-payment 输出为 JSON 数组，测试统一解析后断言。
"""

from datetime import date, timedelta
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]

TODAY = date.today().isoformat()


class CLIHarness:
    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ledger-invoice-test-")
        self.db_path = str(Path(self.tmp) / "ledger.db")

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "CONTRACT_LEDGER_DB": self.db_path}
        return subprocess.run(
            [sys.executable, "-m", "contract_ledger", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def register_contract(self, code="HT1", customer="甲方公司",
                          signed="2026-01-15", amount="10000"):
        return self.invoke(
            "register-contract", "--code", code, "--customer", customer,
            "--date", signed, "--amount", amount,
        )

    def register_milestone(self, contract="HT1", code="MS1", name="首付款",
                           amount="4000", due="2026-03-01"):
        return self.invoke(
            "register-milestone", "--contract", contract, "--code", code,
            "--name", name, "--amount", amount, "--due", due,
        )

    def issue_invoice(self, contract="HT1", invoice="INV1", milestone="MS1",
                      amount="4000", date="2026-02-01", flag="--code"):
        return self.invoke(
            "issue-invoice", "--contract", contract, flag, invoice,
            "--milestone", milestone, "--amount", amount, "--date", date,
        )

    def record_payment(self, invoice="INV1", code="PAY1",
                       amount="1000", date="2026-02-10"):
        return self.invoke(
            "record-payment", "--invoice", invoice, "--code", code,
            "--amount", amount, "--date", date,
        )

    def void_invoice(self, invoice: str):
        return self.invoke("void-invoice", "--invoice", invoice)

    def query_payment(self, contract="HT1", as_of: str | None = None):
        args = ["query-payment", "--contract", contract]
        if as_of is not None:
            args += ["--as-of", as_of]
        return self.invoke(*args)

    def query_json(self, contract="HT1", as_of: str | None = None):
        result = self.query_payment(contract, as_of)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def invoice_rows(self, contract="HT1", as_of: str | None = None):
        payload = self.query_json(contract, as_of)
        return payload[:-1], payload[-1]


class InvoiceRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)

    def test_issue_success_outputs_invoice_milestone_amount(self) -> None:
        result = self.cli.issue_invoice(amount="4000.50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("发票号：INV1", result.stdout)
        self.assertIn("里程碑编号：MS1", result.stdout)
        self.assertIn("金额：4000.5", result.stdout)

    def test_accepts_invoice_flag_alias(self) -> None:
        result = self.cli.issue_invoice(invoice="INV-A", flag="--invoice")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INV-A", result.stdout)

    def test_accepts_positional_invoice_number(self) -> None:
        result = self.cli.invoke(
            "issue-invoice", "--contract", "HT1", "INV-POS",
            "--milestone", "MS1", "--amount", "100", "--date", "2026-02-01",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("发票号：INV-POS", result.stdout)

    def test_duplicate_invoice_rejected_and_keeps_data(self) -> None:
        self.assertEqual(self.cli.issue_invoice().returncode, 0)
        dup = self.cli.issue_invoice(invoice="INV1", amount="999")
        self.assertNotEqual(dup.returncode, 0)
        self.assertIn("INV1", dup.stderr)
        # 既有发票金额未被改动。
        rows, _ = self.cli.invoice_rows()
        self.assertEqual(rows[0]["amount"], 4000)

    def test_non_positive_or_three_decimal_amount_rejected(self) -> None:
        for bad in ["0", "-1", "0.00", "1.234", "abc", "1e3"]:
            with self.subTest(bad=bad):
                result = self.cli.issue_invoice(invoice=f"X-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("金额", result.stderr)

    def test_date_before_contract_signed_rejected(self) -> None:
        result = self.cli.issue_invoice(date="2026-01-14")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("签订日期", result.stderr)

    def test_date_equal_to_contract_signed_allowed(self) -> None:
        result = self.cli.issue_invoice(date="2026-01-15")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_contract_rejected(self) -> None:
        result = self.cli.issue_invoice(contract="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_unknown_milestone_rejected(self) -> None:
        result = self.cli.issue_invoice(milestone="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("里程碑编号不存在", result.stderr)

    def test_milestone_not_belonging_to_contract_rejected(self) -> None:
        self.cli.register_contract(code="HT2", customer="乙", amount="100")
        result = self.cli.issue_invoice(contract="HT2", invoice="INV9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不属于该合同", result.stderr)
        # 第二份合同下没有任何发票，超额合计为 0。
        rows, summary = self.cli.invoice_rows("HT2")
        self.assertEqual(rows, [])
        self.assertEqual(summary["totalExcess"], 0)

    def test_failed_issue_leaves_no_invoice(self) -> None:
        before = self.cli.query_payment().stdout
        self.cli.issue_invoice(date="2020-01-01", invoice="LATER")
        after = self.cli.query_payment().stdout
        self.assertEqual(before, after)


class PaymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.cli.register_contract()
        self.cli.register_milestone()
        self.assertEqual(
            self.cli.issue_invoice(amount="1000").returncode, 0
        )

    def test_partial_payment_keeps_invoice_unpaid(self) -> None:
        result = self.cli.record_payment(amount="300")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows, summary = self.cli.invoice_rows()
        row = rows[0]
        self.assertEqual(row["status"], "unpaid")
        self.assertEqual(row["received"], 300)
        self.assertEqual(row["excess"], 0)
        self.assertEqual(summary["totalExcess"], 0)

    def test_exact_payment_marks_paid(self) -> None:
        self.cli.record_payment(code="P1", amount="400.50", date="2026-02-10")
        self.cli.record_payment(code="P2", amount="599.50", date="2026-02-11")
        rows, _ = self.cli.invoice_rows()
        self.assertEqual(rows[0]["status"], "paid")
        self.assertEqual(rows[0]["received"], 1000)

    def test_overpayment_applies_invoice_amount_and_balances_excess(self) -> None:
        result = self.cli.record_payment(amount="1200")
        self.assertEqual(result.returncode, 0, result.stderr)
        # 入账 1000，超出 200 转记合同超额余额，不丢弃。
        self.assertIn("入账金额：1000", result.stdout)
        self.assertIn("超收金额：200", result.stdout)
        rows, summary = self.cli.invoice_rows()
        self.assertEqual(rows[0]["status"], "paid")
        self.assertEqual(rows[0]["received"], 1000)
        self.assertEqual(rows[0]["excess"], 200)
        self.assertEqual(summary["totalExcess"], 200)

    def test_excess_accumulates_after_invoice_paid(self) -> None:
        self.cli.record_payment(code="P1", amount="1000")
        # 已收齐，后续整笔都转超额。
        self.cli.record_payment(code="P2", amount="300")
        self.cli.record_payment(code="P3", amount="50")
        rows, summary = self.cli.invoice_rows()
        self.assertEqual(rows[0]["excess"], 350)
        self.assertEqual(summary["totalExcess"], 350)

    def test_excess_spans_multiple_invoices_of_contract(self) -> None:
        self.cli.register_milestone(code="MS2", name="尾款",
                                   amount="500", due="2026-04-01")
        self.cli.issue_invoice(invoice="INV2", milestone="MS2",
                               amount="500", date="2026-03-01")
        # INV1 超 200；INV2 恰好收齐不产生超额。
        self.cli.record_payment(invoice="INV1", code="P1", amount="1200")
        self.cli.record_payment(invoice="INV2", code="P2", amount="500")
        rows, summary = self.cli.invoice_rows()
        self.assertEqual([r["invoice"] for r in rows], ["INV1", "INV2"])  # 登记先后
        self.assertEqual(rows[1]["milestone"], "MS2")
        self.assertEqual(summary["totalExcess"], 200)

    def test_duplicate_payment_code_rejected(self) -> None:
        self.cli.record_payment(code="DUP", amount="10")
        dup = self.cli.record_payment(code="DUP", amount="20")
        self.assertNotEqual(dup.returncode, 0)
        self.assertIn("DUP", dup.stderr)
        rows, _ = self.cli.invoice_rows()
        self.assertEqual(rows[0]["received"], 10)

    def test_unknown_invoice_rejected(self) -> None:
        result = self.cli.record_payment(invoice="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_payment_date_before_invoice_date_rejected(self) -> None:
        result = self.cli.record_payment(date="2026-01-31")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("发票日期", result.stderr)
        rows, _ = self.cli.invoice_rows()
        self.assertEqual(rows[0]["received"], 0)

    def test_non_positive_amount_rejected(self) -> None:
        for bad in ["0", "-100", "1.999"]:
            with self.subTest(bad=bad):
                result = self.cli.record_payment(code=f"Z-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)

    def test_failed_payment_changes_nothing(self) -> None:
        self.cli.record_payment(code="P1", amount="400")
        before = self.cli.query_payment().stdout
        # 重复单号 + 大额：即便会产生超额，也必须整体回滚。
        bad = self.cli.record_payment(code="P1", amount="9999")
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(self.cli.query_payment().stdout, before)

    def test_query_is_read_only(self) -> None:
        self.cli.record_payment(amount="10")
        first = self.cli.query_payment().stdout
        second = self.cli.query_payment().stdout
        self.assertEqual(first, second)

    def test_query_unknown_contract_fails(self) -> None:
        result = self.cli.query_payment("NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)
        self.assertEqual(result.stdout, "")


class VoidInvoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.cli.register_contract()
        self.cli.register_milestone()
        self.cli.issue_invoice(amount="1000")

    def test_void_unpaid_success(self) -> None:
        result = self.cli.void_invoice("INV1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("发票已作废：INV1", result.stdout)
        self.assertIn("状态：void", result.stdout)
        rows, _ = self.cli.invoice_rows()
        self.assertEqual(rows[0]["status"], "void")

    def test_paid_invoice_cannot_be_voided(self) -> None:
        self.assertEqual(
            self.cli.record_payment(code="P1", amount="1000").returncode, 0
        )
        result = self.cli.void_invoice("INV1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("paid", result.stderr)
        rows, _ = self.cli.invoice_rows()
        self.assertEqual(rows[0]["status"], "paid")

    def test_void_invoice_cannot_be_voided_again(self) -> None:
        self.assertEqual(self.cli.void_invoice("INV1").returncode, 0)
        again = self.cli.void_invoice("INV1")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("已作废", again.stderr)

    def test_unknown_invoice_cannot_be_voided(self) -> None:
        result = self.cli.void_invoice("GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_void_keeps_received_excess_and_contract_balance(self) -> None:
        # unpaid 发票也可能有部分收款（300），作废前后金额字段必须一致。
        self.cli.record_payment(code="P1", amount="300")
        before_rows, before_summary = self.cli.invoice_rows()
        self.assertEqual(self.cli.void_invoice("INV1").returncode, 0)
        after_rows, after_summary = self.cli.invoice_rows()
        self.assertEqual(before_rows[0]["received"], after_rows[0]["received"])
        self.assertEqual(before_rows[0]["excess"], after_rows[0]["excess"])
        self.assertEqual(
            before_summary["totalExcess"], after_summary["totalExcess"]
        )
        self.assertEqual(after_rows[0]["received"], 300)
        self.assertEqual(after_rows[0]["status"], "void")

    def test_voided_invoice_rejects_further_payments(self) -> None:
        self.assertEqual(self.cli.void_invoice("INV1").returncode, 0)
        result = self.cli.record_payment(code="P1", amount="100")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("作废", result.stderr)
        rows, _ = self.cli.invoice_rows()
        self.assertEqual(rows[0]["status"], "void")
        self.assertEqual(rows[0]["received"], 0)

    def test_failed_void_changes_nothing(self) -> None:
        # 发票不存在时不得改动任何数据。
        before = self.cli.query_payment().stdout
        bad = self.cli.void_invoice("GHOST")
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(self.cli.query_payment().stdout, before)


class QueryPaymentJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.cli.register_contract()
        # MS1 到期 2026-03-01，MS2 到期 2026-04-01。
        self.cli.register_milestone(code="MS1", due="2026-03-01")
        self.cli.register_milestone(code="MS2", name="尾款", amount="500",
                                   due="2026-04-01")
        self.cli.issue_invoice(invoice="INV1", milestone="MS1",
                               amount="1000", date="2026-02-01")
        self.cli.issue_invoice(invoice="INV2", milestone="MS2",
                               amount="500", date="2026-03-01")

    def test_payload_schema_is_fixed(self) -> None:
        payload = self.cli.query_json()
        invoice_keys = {
            "invoice", "milestone", "amount", "status", "received",
            "excess", "overdueDays", "overdueAmount",
        }
        for row in payload[:-1]:
            self.assertEqual(set(row), invoice_keys)
        self.assertEqual(
            set(payload[-1]), {"totalExcess", "totalOverdueAmount"}
        )
        self.assertEqual(len(payload), 3)  # 2 张发票 + 1 个汇总

    def test_amounts_are_numbers(self) -> None:
        self.cli.record_payment(invoice="INV1", code="P1", amount="300.5")
        rows, _ = self.cli.invoice_rows()
        row = rows[0]
        self.assertIsInstance(row["amount"], (int, float))
        self.assertIsInstance(row["received"], (int, float))
        self.assertEqual(row["amount"], 1000)
        self.assertEqual(row["received"], 300.5)

    def test_overdue_days_boundary(self) -> None:
        rows, _ = self.cli.invoice_rows(as_of="2026-03-01")
        # 观察日恰为到期日 -> 0。
        self.assertEqual(rows[0]["overdueDays"], 0)
        rows, _ = self.cli.invoice_rows(as_of="2026-02-28")
        self.assertEqual(rows[0]["overdueDays"], 0)  # 早于到期日
        rows, _ = self.cli.invoice_rows(as_of="2026-03-02")
        self.assertEqual(rows[0]["overdueDays"], 1)  # 晚一天

    def test_overdue_amount_only_for_unpaid(self) -> None:
        # INV1 部分收款 300；INV2 收齐 paid；INV1 逾期、INV2 未逾期时：
        self.cli.record_payment(invoice="INV1", code="P1", amount="300",
                                date="2026-02-10")
        self.cli.record_payment(invoice="INV2", code="P2", amount="500",
                                date="2026-03-05")
        rows, summary = self.cli.invoice_rows(as_of="2026-03-20")
        by_invoice = {r["invoice"]: r for r in rows}
        self.assertEqual(by_invoice["INV1"]["status"], "unpaid")
        self.assertEqual(by_invoice["INV1"]["overdueAmount"], 700)
        self.assertEqual(by_invoice["INV1"]["overdueDays"], 19)
        self.assertEqual(by_invoice["INV2"]["status"], "paid")
        self.assertEqual(by_invoice["INV2"]["overdueAmount"], 0)
        # paid 发票即使逾期也不计逾期金额。
        self.assertEqual(summary["totalOverdueAmount"], 700)

    def test_void_invoice_overdue_amount_is_zero(self) -> None:
        self.assertEqual(self.cli.void_invoice("INV1").returncode, 0)
        rows, summary = self.cli.invoice_rows(as_of="2026-05-01")
        by_invoice = {r["invoice"]: r for r in rows}
        row = by_invoice["INV1"]
        self.assertEqual(row["status"], "void")
        self.assertGreater(row["overdueDays"], 0)  # 逾期天数照常计算
        self.assertEqual(row["overdueAmount"], 0)  # 但逾期金额为 0
        # INV2 仍为 unpaid 且已逾期（500）；作废的 INV1 不贡献逾期金额。
        self.assertEqual(by_invoice["INV2"]["overdueAmount"], 500)
        self.assertEqual(summary["totalOverdueAmount"], 500)

    def test_default_as_of_is_today(self) -> None:
        explicit = self.cli.query_json(as_of=TODAY)
        implicit = self.cli.query_json()
        self.assertEqual(explicit, implicit)

    def test_invalid_as_of_fails_with_empty_stdout(self) -> None:
        for bad in ["2026-13-01", "2026/03/01", "20260301", "2026-2-30", "abc"]:
            with self.subTest(bad=bad):
                result = self.cli.query_payment(as_of=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("观察日期", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_bad_as_of_does_not_modify_data(self) -> None:
        before = self.cli.query_payment().stdout
        self.cli.query_payment(as_of="not-a-date")
        after = self.cli.query_payment().stdout
        self.assertEqual(before, after)

    def test_unknown_contract_with_as_of_fails(self) -> None:
        result = self.cli.query_payment("NOPE", as_of="2026-05-01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)
        self.assertEqual(result.stdout, "")


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)

    def test_invoice_and_payments_persist_across_processes(self) -> None:
        self.cli.register_contract()
        self.cli.register_milestone()
        self.cli.issue_invoice(amount="1000")
        self.cli.record_payment(code="P1", amount="1200")
        # 全新进程读取。
        result = self.cli.query_payment()
        self.assertEqual(result.returncode, 0, result.stderr)
        rows, summary = self.cli.invoice_rows()
        self.assertEqual(rows[0]["status"], "paid")
        self.assertEqual(rows[0]["received"], 1000)
        self.assertEqual(summary["totalExcess"], 200)


if __name__ == "__main__":
    unittest.main()
