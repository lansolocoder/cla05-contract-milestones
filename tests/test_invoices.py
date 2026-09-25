"""开票与收款的端到端 CLI 测试。

每个用例使用独立临时数据库（CONTRACT_LEDGER_DB），与其他测试隔离。
金额一律按整数分比较，日期按 YYYY-MM-DD 文本比较。
"""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


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

    def query_payment(self, contract="HT1"):
        return self.invoke("query-payment", "--contract", contract)


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
        query = self.cli.query_payment()
        self.assertIn("金额：4000", query.stdout)

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
        # 第二份合同下没有任何发票。
        query = self.cli.query_payment("HT2")
        self.assertIn("累计超额收款余额：0", query.stdout)

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
        query = self.cli.query_payment().stdout
        self.assertIn("状态：unpaid", query)
        self.assertIn("已收金额：300", query)
        self.assertIn("超收余额：0", query)
        self.assertIn("累计超额收款余额：0", query)

    def test_exact_payment_marks_paid(self) -> None:
        self.cli.record_payment(code="P1", amount="400.50", date="2026-02-10")
        self.cli.record_payment(code="P2", amount="599.50", date="2026-02-11")
        query = self.cli.query_payment().stdout
        self.assertIn("状态：paid", query)
        self.assertIn("已收金额：1000", query)

    def test_overpayment_applies_invoice_amount_and_balances_excess(self) -> None:
        result = self.cli.record_payment(amount="1200")
        self.assertEqual(result.returncode, 0, result.stderr)
        # 入账 1000，超出 200 转记合同超额余额，不丢弃。
        self.assertIn("入账金额：1000", result.stdout)
        self.assertIn("超收金额：200", result.stdout)
        query = self.cli.query_payment().stdout
        self.assertIn("状态：paid", query)
        self.assertIn("已收金额：1000", query)
        self.assertIn("超收余额：200", query)
        self.assertIn("累计超额收款余额：200", query)

    def test_excess_accumulates_after_invoice_paid(self) -> None:
        self.cli.record_payment(code="P1", amount="1000")
        # 已收齐，后续整笔都转超额。
        self.cli.record_payment(code="P2", amount="300")
        self.cli.record_payment(code="P3", amount="50")
        query = self.cli.query_payment().stdout
        self.assertIn("超收余额：350", query)
        self.assertIn("累计超额收款余额：350", query)

    def test_excess_spans_multiple_invoices_of_contract(self) -> None:
        self.cli.register_milestone(code="MS2", name="尾款",
                                   amount="500", due="2026-04-01")
        self.cli.issue_invoice(invoice="INV2", milestone="MS2",
                               amount="500", date="2026-03-01")
        # INV1 超 200；INV2 恰好收齐不产生超额。
        self.cli.record_payment(invoice="INV1", code="P1", amount="1200")
        self.cli.record_payment(invoice="INV2", code="P2", amount="500")
        query = self.cli.query_payment().stdout
        lines = [ln for ln in query.splitlines() if ln.startswith("INV")]
        self.assertEqual(len(lines), 2)
        self.assertLess(query.index("INV1"), query.index("INV2"))  # 登记先后
        self.assertIn("INV2  MS2", lines[1])
        self.assertTrue(query.rstrip().endswith("累计超额收款余额：200"))

    def test_duplicate_payment_code_rejected(self) -> None:
        self.cli.record_payment(code="DUP", amount="10")
        dup = self.cli.record_payment(code="DUP", amount="20")
        self.assertNotEqual(dup.returncode, 0)
        self.assertIn("DUP", dup.stderr)
        query = self.cli.query_payment().stdout
        self.assertIn("已收金额：10", query)

    def test_unknown_invoice_rejected(self) -> None:
        result = self.cli.record_payment(invoice="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_payment_date_before_invoice_date_rejected(self) -> None:
        result = self.cli.record_payment(date="2026-01-31")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("发票日期", result.stderr)
        self.assertIn("已收金额：0", self.cli.query_payment().stdout)

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

    def _void(self, invoice: str = "INV1") -> None:
        import sqlite3
        conn = sqlite3.connect(self.cli.db_path)
        try:
            conn.execute("UPDATE invoices SET status = 'void' WHERE code = ?", (invoice,))
            conn.commit()
        finally:
            conn.close()

    def test_void_invoice_rejects_payment(self) -> None:
        self._void()
        result = self.cli.record_payment(code="P1", amount="100")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("作废", result.stderr)
        query = self.cli.query_payment().stdout
        self.assertIn("状态：void", query)
        self.assertIn("已收金额：0", query)

    def test_void_invoice_listed_literally(self) -> None:
        self._void()
        query = self.cli.query_payment().stdout
        self.assertIn("状态：void", query)


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
        query = self.cli.query_payment()
        self.assertEqual(query.returncode, 0, query.stderr)
        self.assertIn("状态：paid", query.stdout)
        self.assertIn("已收金额：1000", query.stdout)
        self.assertIn("累计超额收款余额：200", query.stdout)


if __name__ == "__main__":
    unittest.main()
