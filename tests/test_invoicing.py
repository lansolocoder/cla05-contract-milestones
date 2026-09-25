"""开票登记、收款登记与收款冲销的端到端 CLI 测试。

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
        self.tmp = tempfile.mkdtemp(prefix="ledger-invoicing-test-")
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

    def register_invoice(self, code="INV1", milestone="MS1",
                         amount="2000", date="2026-02-01"):
        return self.invoke(
            "register-invoice",
            "--code", code, "--milestone", milestone,
            "--amount", amount, "--date", date,
        )

    def register_receipt(self, code="RCV1", milestone="MS1",
                         amount="2000", date="2026-02-05"):
        return self.invoke(
            "register-receipt",
            "--code", code, "--milestone", milestone,
            "--amount", amount, "--date", date,
        )

    def reverse_receipt(self, code="RCV1"):
        return self.invoke("reverse-receipt", "--code", code)

    def query(self, code="C001"):
        return self.invoke("query-contract", "--code", code)


class InvoicingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)


class RegisterInvoiceTests(InvoicingTestCase):
    def test_success_outputs_code_and_cumulative(self) -> None:
        result = self.cli.register_invoice(amount="2000.50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("发票编号：INV1", result.stdout)
        self.assertIn("累计开票：2000.50", result.stdout)

    def test_multiple_invoices_accumulate(self) -> None:
        self.assertEqual(self.cli.register_invoice(code="I1", amount="2000").returncode, 0)
        result = self.cli.register_invoice(code="I2", amount="1500.25")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("累计开票：3500.25", result.stdout)

    def test_cumulative_may_exceed_milestone_amount(self) -> None:
        # 里程碑当前金额 4000，累计开票不受其上限约束。
        self.assertEqual(self.cli.register_invoice(code="I1", amount="3000").returncode, 0)
        result = self.cli.register_invoice(code="I2", amount="3000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("累计开票：6000", result.stdout)

    def test_duplicate_code_rejected(self) -> None:
        self.assertEqual(self.cli.register_invoice().returncode, 0)
        again = self.cli.register_invoice(amount="999")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("INV1", again.stderr)
        # 累计开票额保持第一次登记的结果。
        self.assertIn("累计开票：2000", self.cli.query().stdout)

    def test_unknown_milestone_rejected(self) -> None:
        result = self.cli.register_invoice(milestone="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("里程碑编号不存在", result.stderr)
        self.assertIn("累计开票：0", self.cli.query().stdout)

    def test_date_before_due_rejected(self) -> None:
        result = self.cli.register_invoice(date="2026-01-31")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("到期日", result.stderr)
        self.assertIn("累计开票：0", self.cli.query().stdout)

    def test_date_equal_to_due_allowed(self) -> None:
        result = self.cli.register_invoice(date="2026-02-01")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_amount_rejected(self) -> None:
        for bad in ["0", "-1", "1.005", "abc"]:
            with self.subTest(bad=bad):
                result = self.cli.register_invoice(code=f"A-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("开票金额", result.stderr)

    def test_invalid_date_rejected(self) -> None:
        result = self.cli.register_invoice(date="2026/02/01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("开票日期", result.stderr)


class RegisterReceiptTests(InvoicingTestCase):
    def test_success_outputs_code_cumulative_and_overpaid(self) -> None:
        result = self.cli.register_receipt(amount="2000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("收款编号：RCV1", result.stdout)
        self.assertIn("累计收款：2000", result.stdout)
        self.assertIn("超收：0", result.stdout)

    def test_overpayment_recorded_as_balance(self) -> None:
        # 里程碑当前金额 4000，收款 4500 -> 超收 500。
        result = self.cli.register_receipt(amount="4500")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("累计收款：4500", result.stdout)
        self.assertIn("超收：500", result.stdout)

    def test_multiple_receipts_accumulate(self) -> None:
        self.assertEqual(self.cli.register_receipt(code="R1", amount="3000").returncode, 0)
        result = self.cli.register_receipt(code="R2", amount="2000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("累计收款：5000", result.stdout)
        self.assertIn("超收：1000", result.stdout)

    def test_duplicate_code_rejected(self) -> None:
        self.assertEqual(self.cli.register_receipt().returncode, 0)
        again = self.cli.register_receipt(amount="999")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("RCV1", again.stderr)
        self.assertIn("累计收款：2000", self.cli.query().stdout)

    def test_unknown_milestone_rejected(self) -> None:
        result = self.cli.register_receipt(milestone="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("里程碑编号不存在", result.stderr)
        self.assertIn("累计收款：0", self.cli.query().stdout)

    def test_invalid_amount_rejected(self) -> None:
        for bad in ["0", "-1", "1.005", "abc"]:
            with self.subTest(bad=bad):
                result = self.cli.register_receipt(code=f"A-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("收款金额", result.stderr)

    def test_invalid_date_rejected(self) -> None:
        result = self.cli.register_receipt(date="2026-02-30")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("收款日期", result.stderr)


class ReverseReceiptTests(InvoicingTestCase):
    def test_reverse_success_deducts_from_totals(self) -> None:
        # 收款 4500（超收 500）后再收 2000（累计 6500，超收 2500），
        # 冲销第一笔：累计 2000，超收归零。
        self.assertEqual(self.cli.register_receipt(code="R1", amount="4500").returncode, 0)
        self.assertEqual(self.cli.register_receipt(code="R2", amount="2000").returncode, 0)
        result = self.cli.reverse_receipt("R1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("收款编号：R1", result.stdout)
        self.assertIn("超收：0", result.stdout)
        query = self.cli.query().stdout
        self.assertIn("累计收款：2000", query)
        self.assertIn("超收：0", query)

    def test_reverse_partial_overpaid_balance(self) -> None:
        # 两笔各 3000（累计 6000，超收 2000），冲销一笔后累计 3000、超收 0。
        self.assertEqual(self.cli.register_receipt(code="R1", amount="3000").returncode, 0)
        self.assertEqual(self.cli.register_receipt(code="R2", amount="3000").returncode, 0)
        result = self.cli.reverse_receipt("R2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("超收：0", result.stdout)
        self.assertIn("累计收款：3000", self.cli.query().stdout)

    def test_double_reverse_rejected(self) -> None:
        self.assertEqual(self.cli.register_receipt().returncode, 0)
        self.assertEqual(self.cli.reverse_receipt().returncode, 0)
        again = self.cli.reverse_receipt()
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("不得重复冲销", again.stderr)
        # 累计额与超收保持首次冲销后的结果。
        query = self.cli.query().stdout
        self.assertIn("累计收款：0", query)
        self.assertIn("超收：0", query)

    def test_unknown_code_rejected(self) -> None:
        result = self.cli.reverse_receipt("NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("收款编号不存在", result.stderr)


class InvoicingQueryTests(InvoicingTestCase):
    def test_query_line_appends_invoicing_amounts(self) -> None:
        self.assertEqual(self.cli.register_invoice(amount="2000.50").returncode, 0)
        self.assertEqual(self.cli.register_receipt(amount="4500").returncode, 0)
        query = self.cli.query().stdout
        self.assertIn(
            "- MS1  名称：首付款  到期日：2026-02-01  当前金额：4000  "
            "累计开票：2000.50  累计收款：4500  超收：500",
            query,
        )

    def test_query_defaults_to_zero_amounts(self) -> None:
        query = self.cli.query().stdout
        self.assertIn("累计开票：0  累计收款：0  超收：0", query)

    def test_failed_operation_leaves_no_trace(self) -> None:
        before = self.cli.query().stdout
        self.assertNotEqual(self.cli.register_invoice(date="2026-01-01").returncode, 0)
        self.assertNotEqual(self.cli.register_receipt(milestone="GHOST").returncode, 0)
        self.assertNotEqual(self.cli.reverse_receipt("NOPE").returncode, 0)
        self.assertEqual(self.cli.query().stdout, before)


if __name__ == "__main__":
    unittest.main()
