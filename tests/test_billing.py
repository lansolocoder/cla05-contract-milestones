"""开票登记、收款登记与收款冲销的端到端 CLI 测试。

每个用例使用独立临时数据库（CONTRACT_LEDGER_DB），测试结束后清理。
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
        self.tmp = tempfile.mkdtemp(prefix="ledger-billing-test-")
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

    def register_contract(self, code="C001", amount="10000"):
        return self.invoke(
            "register-contract", "--code", code, "--customer", "甲方",
            "--date", "2026-01-15", "--amount", amount,
        )

    def register_milestone(self, contract="C001", code="MS1",
                           amount="4000", due="2026-03-01"):
        return self.invoke(
            "register-milestone", "--contract", contract, "--code", code,
            "--name", "首付款", "--amount", amount, "--due", due,
        )

    def register_change(self, contract="C001", code="CHG1",
                        delta="100", milestone=None):
        args = [
            "register-change", "--contract", contract, "--code", code,
            "--description", "调价", "--delta", delta,
        ]
        if milestone is not None:
            args += ["--milestone", milestone]
        return self.invoke(*args)

    def effect(self, code):
        return self.invoke("effect-change", "--code", code)

    def invoice(self, invoice, milestone="MS1", amount="1000",
                date_="2026-03-01"):
        return self.invoke(
            "register-invoice", "--invoice", invoice, "--milestone",
            milestone, "--amount", amount, "--date", date_,
        )

    def receipt(self, receipt, milestone="MS1", amount="1000",
                date_="2026-03-05"):
        return self.invoke(
            "register-receipt", "--receipt", receipt, "--milestone",
            milestone, "--amount", amount, "--date", date_,
        )

    def reverse(self, receipt):
        return self.invoke("reverse-receipt", "--receipt", receipt)

    def query(self, code="C001"):
        return self.invoke("query-contract", "--code", code)


class BillingFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)


class InvoiceTests(BillingFixture):
    def test_invoice_success_outputs_code_and_total(self) -> None:
        result = self.cli.invoice("INV1", amount="3000.50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("发票编号：INV1", result.stdout)
        self.assertIn("累计开票：3000.50", result.stdout)

    def test_multiple_invoices_accumulate_without_cap(self) -> None:
        self.assertEqual(self.cli.invoice("INV1", amount="3000").returncode, 0)
        # 里程碑当前金额 4000，累计开票可超过且继续累加。
        result = self.cli.invoice("INV2", amount="2500.75", date_="2026-03-02")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("累计开票：5500.75", result.stdout)

    def test_invoice_date_equal_to_due_allowed(self) -> None:
        result = self.cli.invoice("INV1", date_="2026-03-01")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invoice_before_due_rejected(self) -> None:
        result = self.cli.invoice("INV1", date_="2026-02-28")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("到期日", result.stderr)
        self.assertIn("累计开票：0", self.cli.query().stdout)

    def test_invoice_unknown_milestone_rejected(self) -> None:
        result = self.cli.invoice("INV1", milestone="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("里程碑编号不存在", result.stderr)

    def test_duplicate_invoice_code_rejected_globally(self) -> None:
        self.assertEqual(self.cli.invoice("INV1").returncode, 0)
        self.cli.register_contract(code="C002", amount="100")
        self.cli.register_milestone(contract="C002", code="MS2",
                                   amount="100", due="2026-03-01")
        result = self.cli.invoice("INV1", milestone="MS2", date_="2026-03-02")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INV1", result.stderr)
        # 第二个里程碑累计开票仍为零，失败整体回滚。
        self.assertIn(
            "- MS2  名称：首付款  到期日：2026-03-01  当前金额：100  "
            "累计开票：0",
            self.cli.query("C002").stdout,
        )

    def test_invoice_bad_amount_rejected(self) -> None:
        for bad in ["0", "-1", "1.005"]:
            with self.subTest(bad=bad):
                result = self.cli.invoice(f"X-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("金额", result.stderr)

    def test_invoice_bad_date_rejected(self) -> None:
        result = self.cli.invoice("INV1", date_="2026-13-01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("开票日期", result.stderr)


class ReceiptTests(BillingFixture):
    def test_receipt_within_amount_has_zero_overreceipt(self) -> None:
        result = self.cli.receipt("R1", amount="4000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("收款编号：R1", result.stdout)
        self.assertIn("累计收款：4000", result.stdout)
        self.assertIn("超收余额：0", result.stdout)

    def test_receipt_over_amount_records_overreceipt(self) -> None:
        result = self.cli.receipt("R1", amount="4500.25")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("累计收款：4500.25", result.stdout)
        self.assertIn("超收余额：500.25", result.stdout)

    def test_overreceipt_accumulates_across_receipts(self) -> None:
        self.assertEqual(self.cli.receipt("R1", amount="3000").returncode, 0)
        result = self.cli.receipt("R2", amount="2500")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("累计收款：5500", result.stdout)
        self.assertIn("超收余额：1500", result.stdout)

    def test_overreceipt_tracks_current_milestone_amount(self) -> None:
        # 里程碑 4000，收款 3500 不超收；生效 -1000 变更后当前 3000，
        # 超收余额变为 500。
        self.assertEqual(self.cli.receipt("R1", amount="3500").returncode, 0)
        self.assertEqual(
            self.cli.register_change(code="CH1", delta="-1000",
                                     milestone="MS1").returncode,
            0,
        )
        self.assertEqual(self.cli.effect("CH1").returncode, 0)
        query = self.cli.query().stdout
        self.assertIn("当前金额：3000  累计开票：0  累计收款：3500  超收：500",
                      query)

    def test_unknown_milestone_rejected(self) -> None:
        result = self.cli.receipt("R1", milestone="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("里程碑编号不存在", result.stderr)
        self.assertIn("累计收款：0", self.cli.query().stdout)

    def test_duplicate_receipt_code_rejected_globally(self) -> None:
        self.assertEqual(self.cli.receipt("R1").returncode, 0)
        self.cli.register_contract(code="C002", amount="100")
        self.cli.register_milestone(contract="C002", code="MS2",
                                   amount="100", due="2026-03-01")
        result = self.cli.receipt("R1", milestone="MS2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("R1", result.stderr)
        self.assertIn("累计收款：0", self.cli.query("C002").stdout)

    def test_bad_amount_and_date_rejected(self) -> None:
        result = self.cli.receipt("R1", amount="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("金额", result.stderr)
        result = self.cli.receipt("R1", amount="1.005")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("金额", result.stderr)
        result = self.cli.receipt("R1", date_="2026/03/05")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("收款日期", result.stderr)


class ReverseReceiptTests(BillingFixture):
    def test_reverse_deducts_from_total(self) -> None:
        self.assertEqual(self.cli.receipt("R1", amount="1000").returncode, 0)
        result = self.cli.reverse("R1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("收款编号：R1", result.stdout)
        self.assertIn("超收余额：0", result.stdout)
        self.assertIn("累计收款：0", self.cli.query().stdout)

    def test_reverse_reduces_overreceipt(self) -> None:
        self.assertEqual(self.cli.receipt("R1", amount="4500").returncode, 0)
        result = self.cli.reverse("R1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("超收余额：0", result.stdout)

    def test_reverse_partial_overreceipt_floored_at_zero(self) -> None:
        # 两笔共 5500（超收 1500）；冲销 2500 那笔后累计 3000，超收 0。
        self.assertEqual(self.cli.receipt("R1", amount="3000").returncode, 0)
        self.assertEqual(self.cli.receipt("R2", amount="2500").returncode, 0)
        result = self.cli.reverse("R2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("超收余额：0", result.stdout)
        query = self.cli.query().stdout
        self.assertIn("累计收款：3000  超收：0", query)

    def test_reverse_keeps_overreceipt_when_still_over(self) -> None:
        # 两笔共 6000（超收 2000）；冲销 1000 后累计 5000，仍超收 1000。
        self.assertEqual(self.cli.receipt("R1", amount="5000").returncode, 0)
        self.assertEqual(self.cli.receipt("R2", amount="1000").returncode, 0)
        result = self.cli.reverse("R2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("超收余额：1000", result.stdout)

    def test_double_reverse_rejected(self) -> None:
        self.assertEqual(self.cli.receipt("R1").returncode, 0)
        self.assertEqual(self.cli.reverse("R1").returncode, 0)
        result = self.cli.reverse("R1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不得再次冲销", result.stderr)
        self.assertIn("累计收款：0", self.cli.query().stdout)

    def test_reverse_unknown_receipt_rejected(self) -> None:
        result = self.cli.reverse("GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("收款编号不存在", result.stderr)

    def test_reversed_receipt_excluded_but_other_receipts_kept(self) -> None:
        self.assertEqual(self.cli.receipt("R1", amount="1000").returncode, 0)
        self.assertEqual(self.cli.receipt("R2", amount="2000").returncode, 0)
        self.assertEqual(self.cli.reverse("R1").returncode, 0)
        query = self.cli.query().stdout
        self.assertIn("累计收款：2000", query)
        self.assertIn("超收：0", query)


if __name__ == "__main__":
    unittest.main()
