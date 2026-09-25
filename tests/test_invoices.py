"""按里程碑开票与收款登记的端到端 CLI 测试。

与 test_milestones.py 相同：每个用例使用独立临时数据库
（CONTRACT_LEDGER_DB），测试结束后清理。
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

    def register_invoice(self, invoice="INV1", contract="C001", milestone="MS1",
                         amount="4000", date="2026-02-10"):
        return self.invoke(
            "register-invoice",
            "--invoice", invoice, "--contract", contract,
            "--milestone", milestone, "--amount", amount, "--date", date,
        )

    def register_receipt(self, receipt="RCPT1", invoice="INV1",
                         amount="1000", date="2026-02-15"):
        return self.invoke(
            "register-receipt",
            "--receipt", receipt, "--invoice", invoice,
            "--amount", amount, "--date", date,
        )

    def query(self, code="C001"):
        return self.invoke("query-contract", "--code", code)


class InvoiceRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)

    def test_register_success_outputs_code_and_status(self) -> None:
        result = self.cli.register_invoice(amount="4000.50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("发票号：INV1", result.stdout)
        self.assertIn("状态：未收清", result.stdout)

    def test_duplicate_invoice_rejected_globally(self) -> None:
        self.assertEqual(self.cli.register_invoice().returncode, 0)
        self.cli.register_contract(code="C002", customer="乙", amount="100")
        self.cli.register_milestone(contract="C002", code="MS2", name="尾款",
                                    amount="100", due="2026-03-01")
        second = self.cli.register_invoice(contract="C002", milestone="MS2")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("INV1", second.stderr)
        self.assertIn("发票：无", self.cli.query("C002").stdout)

    def test_non_positive_amount_rejected(self) -> None:
        for bad in ["0", "-1", "0.00"]:
            with self.subTest(bad=bad):
                result = self.cli.register_invoice(invoice=f"A-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("金额", result.stderr)

    def test_amount_too_many_decimals_rejected(self) -> None:
        result = self.cli.register_invoice(amount="1.005")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("金额", result.stderr)

    def test_invalid_date_rejected(self) -> None:
        for bad in ["2026-13-01", "2026/02/10", "20260210", "2026-2-10"]:
            with self.subTest(bad=bad):
                result = self.cli.register_invoice(date=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("开票日期", result.stderr)

    def test_date_before_signed_date_rejected(self) -> None:
        result = self.cli.register_invoice(date="2026-01-14")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("签订日期", result.stderr)
        self.assertIn("发票：无", self.cli.query().stdout)

    def test_date_equal_to_signed_date_allowed(self) -> None:
        result = self.cli.register_invoice(date="2026-01-15")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_contract_rejected(self) -> None:
        result = self.cli.register_invoice(contract="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_unknown_milestone_rejected(self) -> None:
        result = self.cli.register_invoice(milestone="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("里程碑编号不存在", result.stderr)

    def test_milestone_of_other_contract_rejected(self) -> None:
        self.cli.register_contract(code="C002", customer="乙", amount="100")
        result = self.cli.register_invoice(contract="C002")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不属于", result.stderr)
        self.assertIn("发票：无", self.cli.query("C002").stdout)


class ReceiptRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(
            self.cli.register_invoice(amount="4000", date="2026-02-10").returncode,
            0,
        )

    def test_partial_receipt_keeps_unpaid(self) -> None:
        result = self.cli.register_receipt(amount="1500.25")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("收款单号：RCPT1", result.stdout)
        self.assertIn("状态：未收清", result.stdout)

    def test_full_receipt_marks_paid(self) -> None:
        result = self.cli.register_receipt(amount="4000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("状态：已收清", result.stdout)

    def test_multiple_receipts_accumulate(self) -> None:
        self.assertEqual(
            self.cli.register_receipt(receipt="R1", amount="1000").returncode, 0
        )
        self.assertEqual(
            self.cli.register_receipt(receipt="R2", amount="2000").returncode, 0
        )
        result = self.cli.register_receipt(receipt="R3", amount="1000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("状态：已收清", result.stdout)

    def test_receipt_after_paid_rejected(self) -> None:
        self.assertEqual(self.cli.register_receipt(amount="4000").returncode, 0)
        result = self.cli.register_receipt(receipt="R2", amount="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("已收清", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_overpayment_rejected_and_not_written(self) -> None:
        self.assertEqual(self.cli.register_receipt(amount="3000").returncode, 0)
        result = self.cli.register_receipt(receipt="R2", amount="1000.01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不得超过开票金额", result.stderr)
        self.assertEqual(result.stdout, "")
        # 既有收款不受影响，仍允许补足差额。
        result = self.cli.register_receipt(receipt="R3", amount="1000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("状态：已收清", result.stdout)

    def test_exact_overpayment_boundary(self) -> None:
        # 恰好等于开票金额允许；多一分则拒绝。
        result = self.cli.register_receipt(receipt="R1", amount="4000.01")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.cli.register_receipt(receipt="R2", amount="4000").returncode, 0
        )

    def test_duplicate_receipt_rejected_without_overwrite(self) -> None:
        self.assertEqual(self.cli.register_receipt(amount="1000").returncode, 0)
        result = self.cli.register_receipt(amount="4000")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RCPT1", result.stderr)
        # 原收款保持 1000，发票仍未收清。
        query = self.cli.query().stdout
        self.assertIn("已收款：1000", query)
        self.assertIn("状态：未收清", query)

    def test_unknown_invoice_rejected(self) -> None:
        result = self.cli.register_receipt(invoice="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_non_positive_amount_rejected(self) -> None:
        for bad in ["0", "-1", "0.00"]:
            with self.subTest(bad=bad):
                result = self.cli.register_receipt(receipt=f"A-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("金额", result.stderr)

    def test_amount_too_many_decimals_rejected(self) -> None:
        result = self.cli.register_receipt(amount="1.005")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("金额", result.stderr)

    def test_date_before_invoice_date_rejected(self) -> None:
        result = self.cli.register_receipt(date="2026-02-09")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("开票日期", result.stderr)
        self.assertIn("已收款：0", self.cli.query().stdout)

    def test_date_equal_to_invoice_date_allowed(self) -> None:
        result = self.cli.register_receipt(date="2026-02-10")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_date_rejected(self) -> None:
        result = self.cli.register_receipt(date="2026-02-30")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("收款日期", result.stderr)


class InvoiceQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.cli.register_contract(code="ORD", customer="客户甲",
                                   signed="2026-03-08", amount="1000")
        self.cli.register_milestone(contract="ORD", code="M1", name="首款",
                                    amount="300", due="2026-04-01")
        self.cli.register_milestone(contract="ORD", code="M2", name="尾款",
                                    amount="700", due="2026-05-01")

    def test_no_invoices_shows_none_and_zero_sums(self) -> None:
        result = self.cli.query("ORD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("发票：无", result.stdout)
        self.assertIn(
            "- M1  名称：首款  到期日：2026-04-01  当前金额：300  "
            "已开票：0  已收款：0",
            result.stdout,
        )

    def test_milestone_sums_and_invoice_lines(self) -> None:
        self.cli.register_invoice(invoice="I1", contract="ORD", milestone="M1",
                                  amount="100.50", date="2026-04-02")
        self.cli.register_invoice(invoice="I2", contract="ORD", milestone="M1",
                                  amount="200", date="2026-04-03")
        self.cli.register_receipt(receipt="R1", invoice="I1",
                                  amount="100.50", date="2026-04-05")
        self.cli.register_receipt(receipt="R2", invoice="I2",
                                  amount="50", date="2026-04-06")
        result = self.cli.query("ORD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "- M1  名称：首款  到期日：2026-04-01  当前金额：300  "
            "已开票：300.50  已收款：150.50",
            result.stdout,
        )
        self.assertIn(
            "- M2  名称：尾款  到期日：2026-05-01  当前金额：700  "
            "已开票：0  已收款：0",
            result.stdout,
        )
        self.assertIn("发票：", result.stdout)
        self.assertIn(
            "- I1  开票金额：100.50  已收款：100.50  状态：已收清",
            result.stdout,
        )
        self.assertIn(
            "- I2  开票金额：200  已收款：50  状态：未收清",
            result.stdout,
        )
        # 按登记先后排列。
        self.assertLess(result.stdout.index("- I1"), result.stdout.index("- I2"))

    def test_query_does_not_modify_data(self) -> None:
        self.cli.register_invoice(invoice="I1", contract="ORD", milestone="M1",
                                  amount="100", date="2026-04-02")
        self.cli.register_receipt(receipt="R1", invoice="I1",
                                  amount="40", date="2026-04-05")
        before = self.cli.query("ORD").stdout
        after = self.cli.query("ORD").stdout
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
