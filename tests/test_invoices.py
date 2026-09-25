"""发票登记与收款管理的端到端 CLI 测试。

与 test_ledger.py 相同：每个用例使用独立临时数据库
（CONTRACT_LEDGER_DB），测试结束后清理。
"""

from pathlib import Path
import os
import shutil
import sqlite3
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

    def sql(self, statement: str) -> list[tuple]:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(statement).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    # 便捷封装 ----------------------------------------------------------

    def register_contract(self, code="C001", customer="甲方公司",
                          signed="2026-01-15", amount="10000"):
        return self.invoke(
            "register-contract",
            "--code", code, "--customer", customer,
            "--date", signed, "--amount", amount,
        )

    def register_milestone(self, contract="C001", code="MS1", name="首付款",
                           amount="4000", due="2026-02-01"):
        return self.invoke(
            "register-milestone",
            "--contract", contract, "--code", code,
            "--name", name, "--amount", amount, "--due", due,
        )

    def issue_invoice(self, invoice="INV1", milestone="MS1",
                      amount="4000", date="2026-03-01"):
        return self.invoke(
            "issue-invoice",
            "--contract", invoice, "--milestone", milestone,
            "--amount", amount, "--date", date,
        )

    def record_payment(self, invoice="INV1", code="PAY1",
                       amount="4000", date="2026-03-05"):
        return self.invoke(
            "record-payment",
            "--invoice", invoice, "--code", code,
            "--amount", amount, "--date", date,
        )

    def query_payment(self, contract="C001"):
        return self.invoke("query-payment", "--contract", contract)


class InvoiceRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)

    def test_issue_success_outputs_invoice_milestone_amount(self) -> None:
        result = self.cli.issue_invoice(invoice="INV-1", amount="1234.56")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("发票号：INV-1", result.stdout)
        self.assertIn("里程碑编号：MS1", result.stdout)
        self.assertIn("金额：1234.56", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_new_invoice_is_unpaid(self) -> None:
        self.assertEqual(self.cli.issue_invoice().returncode, 0)
        result = self.cli.query_payment()
        self.assertIn("状态：unpaid", result.stdout)
        self.assertIn("已收金额：0", result.stdout)
        self.assertIn("超收余额：0", result.stdout)
        self.assertIn("累计超额收款余额：0", result.stdout)

    def test_duplicate_invoice_code_rejected_globally(self) -> None:
        self.assertEqual(self.cli.issue_invoice(invoice="DUP").returncode, 0)
        # 第二份合同与里程碑上使用同一发票号，仍须拒绝。
        self.cli.register_contract(code="C002", customer="乙", amount="100")
        self.cli.register_milestone(
            contract="C002", code="MS2", amount="100", due="2026-02-01"
        )
        again = self.cli.issue_invoice(invoice="DUP", milestone="MS2")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("DUP", again.stderr)
        query = self.cli.query_payment("C002")
        self.assertIn("发票：无", query.stdout)

    def test_nonexistent_milestone_rejected(self) -> None:
        result = self.cli.issue_invoice(milestone="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_issue_date_before_signed_date_rejected(self) -> None:
        result = self.cli.issue_invoice(date="2026-01-14")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("签订日期", result.stderr)

    def test_issue_date_equal_to_signed_date_allowed(self) -> None:
        result = self.cli.issue_invoice(date="2026-01-15")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_amount_rejected(self) -> None:
        for bad in ["0", "-1", "1.234", "abc", "0.001"]:
            with self.subTest(bad=bad):
                result = self.cli.issue_invoice(invoice=f"B-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0, f"应拒绝 {bad!r}")
                self.assertIn("金额", result.stderr)

    def test_invalid_date_rejected(self) -> None:
        result = self.cli.issue_invoice(date="2026/03/01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("日期", result.stderr)

    def test_failed_issue_leaves_no_row(self) -> None:
        bad = self.cli.issue_invoice(date="2020-01-01", invoice="NOROW")
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(self.cli.sql("SELECT COUNT(*) FROM invoices"), [(0,)])


class RecordPaymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.cli.register_contract()
        self.cli.register_milestone(amount="1000")
        self.assertEqual(
            self.cli.issue_invoice(invoice="INV1", amount="1000").returncode, 0
        )

    def test_partial_payment_keeps_unpaid(self) -> None:
        result = self.cli.record_payment(code="P1", amount="600")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("状态：unpaid", result.stdout)
        query = self.cli.query_payment()
        self.assertIn("状态：unpaid", query.stdout)
        self.assertIn("已收金额：600", query.stdout)
        self.assertIn("累计超额收款余额：0", query.stdout)

    def test_exact_payment_marks_paid(self) -> None:
        result = self.cli.record_payment(code="P1", amount="1000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("状态：paid", result.stdout)
        self.assertIn("已收金额：1000", result.stdout)

    def test_exact_after_partial_marks_paid(self) -> None:
        self.cli.record_payment(code="P1", amount="300")
        result = self.cli.record_payment(code="P2", amount="700")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("状态：paid", result.stdout)
        query = self.cli.query_payment()
        self.assertIn("状态：paid", query.stdout)
        self.assertIn("已收金额：1000", query.stdout)

    def test_overpayment_caps_received_and_routes_excess_to_contract(self) -> None:
        # 发票 1000，单笔 1300.50：收 1000，超 300.50。
        result = self.cli.record_payment(code="P1", amount="1300.50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("状态：paid", result.stdout)
        query = self.cli.query_payment()
        self.assertIn("状态：paid", query.stdout)
        self.assertIn("已收金额：1000", query.stdout)
        self.assertIn("超收余额：300.50", query.stdout)
        self.assertIn("累计超额收款余额：300.50", query.stdout)

    def test_excess_after_partial_payment(self) -> None:
        # 已收 600，再收 600：收齐 1000，超 200。
        self.cli.record_payment(code="P1", amount="600")
        result = self.cli.record_payment(code="P2", amount="600")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("状态：paid", result.stdout)
        query = self.cli.query_payment()
        self.assertIn("已收金额：1000", query.stdout)
        self.assertIn("超收余额：200", query.stdout)
        self.assertIn("累计超额收款余额：200", query.stdout)

    def test_payment_after_paid_fully_goes_to_overpayment(self) -> None:
        self.cli.record_payment(code="P1", amount="1000")
        result = self.cli.record_payment(code="P2", amount="250")
        self.assertEqual(result.returncode, 0, result.stderr)
        query = self.cli.query_payment()
        self.assertIn("已收金额：1000", query.stdout)
        self.assertIn("超收余额：250", query.stdout)

    def test_multiple_overpayments_accumulate_per_contract(self) -> None:
        # 同一合同两张发票各超收，合同余额为两者之和。
        self.cli.register_milestone(code="MS2", amount="500", due="2026-05-01")
        self.cli.issue_invoice(invoice="INV2", milestone="MS2",
                               amount="500", date="2026-05-02")
        self.cli.record_payment(invoice="INV1", code="P1",
                                amount="1100", date="2026-03-05")  # +100
        self.cli.record_payment(invoice="INV2", code="P2",
                                amount="650", date="2026-05-10")   # +150
        query = self.cli.query_payment()
        self.assertIn("超收余额：100", query.stdout)
        self.assertIn("超收余额：150", query.stdout)
        self.assertIn("累计超额收款余额：250", query.stdout)

    def test_duplicate_payment_code_rejected(self) -> None:
        self.cli.record_payment(code="DUP", amount="10")
        again = self.cli.record_payment(code="DUP", amount="10")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("DUP", again.stderr)
        # 只有第一笔。
        self.assertEqual(self.cli.sql("SELECT COUNT(*) FROM payments"), [(1,)])

    def test_payment_date_before_invoice_date_rejected(self) -> None:
        result = self.cli.record_payment(date="2026-02-28")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("发票日期", result.stderr)
        self.assertEqual(self.cli.sql("SELECT COUNT(*) FROM payments"), [(0,)])

    def test_payment_date_equal_to_invoice_date_allowed(self) -> None:
        result = self.cli.record_payment(date="2026-03-01", amount="1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_nonexistent_invoice_rejected(self) -> None:
        result = self.cli.record_payment(invoice="GHOST", code="P9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_void_invoice_rejects_payment(self) -> None:
        self.cli.sql("UPDATE invoices SET status='void' WHERE code='INV1'")
        result = self.cli.record_payment(code="PV", amount="10")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("作废", result.stderr)
        self.assertEqual(self.cli.sql("SELECT COUNT(*) FROM payments"), [(0,)])

    def test_invalid_payment_amount_rejected(self) -> None:
        for bad in ["0", "-5", "1.999"]:
            with self.subTest(bad=bad):
                result = self.cli.record_payment(code=f"X-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("金额", result.stderr)

    def test_failed_payment_changes_nothing(self) -> None:
        # 一笔超收本会写入余额，但收款单号重复 -> 整体回滚。
        self.cli.record_payment(code="OK", amount="600")
        bad = self.cli.record_payment(code="OK", amount="9999")
        self.assertNotEqual(bad.returncode, 0)
        query = self.cli.query_payment()
        self.assertIn("状态：unpaid", query.stdout)
        self.assertIn("已收金额：600", query.stdout)
        self.assertIn("累计超额收款余额：0", query.stdout)
        self.assertEqual(
            self.cli.sql("SELECT COUNT(*) FROM overpayments"), [(0,)]
        )


class QueryPaymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.cli.register_contract(amount="10000")
        self.cli.register_milestone(code="MS1", amount="4000", due="2026-02-01")
        self.cli.register_milestone(code="MS2", amount="6000", due="2026-05-01")

    def test_unknown_contract_fails(self) -> None:
        result = self.cli.query_payment("NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_no_invoices_shows_zero_balance(self) -> None:
        result = self.cli.query_payment()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("发票：无", result.stdout)
        self.assertIn("累计超额收款余额：0", result.stdout)

    def test_rows_in_registration_order_with_literal_status(self) -> None:
        self.cli.issue_invoice(invoice="I1", milestone="MS1",
                               amount="4000", date="2026-02-05")
        self.cli.issue_invoice(invoice="I2", milestone="MS2",
                               amount="6000", date="2026-05-05")
        self.cli.record_payment(invoice="I1", code="P1",
                                amount="4000", date="2026-02-10")  # paid
        # I2 未付款，保持 unpaid；将其作废以验证 void 字面值。
        self.cli.sql("UPDATE invoices SET status='void' WHERE code='I2'")
        result = self.cli.query_payment()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(result.stdout.index("I1"), result.stdout.index("I2"))
        self.assertIn("里程碑编号：MS1", result.stdout)
        self.assertIn("金额：4000", result.stdout)
        self.assertIn("状态：paid", result.stdout)
        self.assertIn("状态：void", result.stdout)
        self.assertIn("已收金额：4000", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("累计超额收款余额：0"))

    def test_query_does_not_modify_data(self) -> None:
        self.cli.issue_invoice()
        before = self.cli.query_payment().stdout
        after = self.cli.query_payment().stdout
        self.assertEqual(before, after)


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)

    def test_state_persists_across_processes(self) -> None:
        self.cli.register_contract()
        self.cli.register_milestone(amount="1000")
        self.cli.issue_invoice(amount="1000")
        self.cli.record_payment(amount="1200")  # 超 200
        result = self.cli.query_payment()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("状态：paid", result.stdout)
        self.assertIn("已收金额：1000", result.stdout)
        self.assertIn("累计超额收款余额：200", result.stdout)


if __name__ == "__main__":
    unittest.main()
