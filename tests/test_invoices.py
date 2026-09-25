"""开票与收款的端到端 CLI 测试。

与 test_ledger.py 相同：每个用例使用独立临时数据库
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

    def record_invoice(self, contract="C001", code="INV1", amount="4000",
                       date="2026-02-01", milestone=None):
        args = [
            "record-invoice",
            "--contract", contract, "--code", code,
            "--amount", amount, "--date", date,
        ]
        if milestone is not None:
            args += ["--milestone", milestone]
        return self.invoke(*args)

    def record_receipt(self, code="RC1", invoice="INV1", amount="1000",
                       date="2026-02-10"):
        return self.invoke(
            "record-receipt",
            "--code", code, "--invoice", invoice,
            "--amount", amount, "--date", date,
        )

    def query_invoice(self, code="INV1"):
        return self.invoke("query-invoice", "--code", code)


class RecordInvoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = CLIHarness()

    def tearDown(self) -> None:
        self.h.cleanup()

    def test_record_invoice_attached_to_contract(self) -> None:
        self.assertEqual(self.h.register_contract().returncode, 0)
        result = self.h.record_invoice(amount="4000.50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("发票编号：INV1", result.stdout)
        self.assertIn("开票金额：4000.50", result.stdout)

    def test_record_invoice_attached_to_milestone(self) -> None:
        self.h.register_contract()
        self.h.register_milestone()
        result = self.h.record_invoice(milestone="MS1")
        self.assertEqual(result.returncode, 0, result.stderr)
        query = self.h.query_invoice()
        self.assertIn("归属：MS1", query.stdout)

    def test_duplicate_invoice_code_rejected(self) -> None:
        self.h.register_contract()
        self.assertEqual(self.h.record_invoice().returncode, 0)
        result = self.h.record_invoice(amount="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INV1", result.stderr)
        # 既有记录不受影响
        query = self.h.query_invoice()
        self.assertIn("开票金额：4000", query.stdout)

    def test_missing_contract_rejected(self) -> None:
        result = self.h.record_invoice(contract="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NOPE", result.stderr)

    def test_missing_milestone_rejected(self) -> None:
        self.h.register_contract()
        result = self.h.record_invoice(milestone="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NOPE", result.stderr)

    def test_milestone_of_other_contract_rejected(self) -> None:
        self.h.register_contract()
        self.h.register_milestone()
        self.h.register_contract(code="C002")
        result = self.h.record_invoice(contract="C002", milestone="MS1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不属于", result.stderr)

    def test_invalid_amount_and_date_rejected(self) -> None:
        self.h.register_contract()
        for kwargs in [
            {"amount": "0"},
            {"amount": "-5"},
            {"amount": "1.234"},
            {"amount": "abc"},
            {"date": "2026-2-1"},
            {"date": "2026-02-30"},
        ]:
            with self.subTest(kwargs=kwargs):
                result = self.h.record_invoice(**kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotEqual(result.stderr, "")


class RecordReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = CLIHarness()
        self.h.register_contract()
        self.h.record_invoice(amount="4000", date="2026-02-01")

    def tearDown(self) -> None:
        self.h.cleanup()

    def test_record_receipt_success(self) -> None:
        result = self.h.record_receipt(amount="1500.25")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("收款编号：RC1", result.stdout)
        self.assertIn("发票编号：INV1", result.stdout)
        self.assertIn("本次收款：1500.25", result.stdout)
        self.assertIn("累计收款：1500.25", result.stdout)
        self.assertIn("未收余额：2499.75", result.stdout)

    def test_cumulative_total_accumulates(self) -> None:
        self.h.record_receipt(code="RC1", amount="1000")
        result = self.h.record_receipt(code="RC2", amount="500")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("累计收款：1500", result.stdout)
        self.assertIn("未收余额：2500", result.stdout)

    def test_overpayment_allowed_up_to_twenty_percent(self) -> None:
        # 恰好 120% 允许
        result = self.h.record_receipt(amount="4800")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("未收余额：-800", result.stdout)

    def test_overpayment_beyond_twenty_percent_rejected(self) -> None:
        result = self.h.record_receipt(amount="4800.01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("20%", result.stderr)
        # 拒绝后不落库
        query = self.h.query_invoice()
        self.assertIn("累计收款：0", query.stdout)

    def test_cumulative_overpayment_beyond_limit_rejected(self) -> None:
        self.assertEqual(self.h.record_receipt(code="RC1", amount="4000").returncode, 0)
        self.assertEqual(self.h.record_receipt(code="RC2", amount="800").returncode, 0)
        result = self.h.record_receipt(code="RC3", amount="0.01")
        self.assertNotEqual(result.returncode, 0)
        query = self.h.query_invoice()
        self.assertIn("累计收款：4800", query.stdout)

    def test_duplicate_receipt_code_rejected(self) -> None:
        self.assertEqual(self.h.record_receipt().returncode, 0)
        result = self.h.record_receipt(amount="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RC1", result.stderr)
        query = self.h.query_invoice()
        self.assertIn("累计收款：1000", query.stdout)

    def test_missing_invoice_rejected(self) -> None:
        result = self.h.record_receipt(invoice="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NOPE", result.stderr)

    def test_receipt_date_before_invoice_date_rejected(self) -> None:
        result = self.h.record_receipt(date="2026-01-31")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("开票日期", result.stderr)
        query = self.h.query_invoice()
        self.assertIn("累计收款：0", query.stdout)

    def test_receipt_date_equal_to_invoice_date_allowed(self) -> None:
        result = self.h.record_receipt(date="2026-02-01")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_amount_rejected(self) -> None:
        for amount in ["0", "-1", "1.234", "abc"]:
            with self.subTest(amount=amount):
                result = self.h.record_receipt(amount=amount)
                self.assertNotEqual(result.returncode, 0)


class QueryInvoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = CLIHarness()
        self.h.register_contract()
        self.h.record_invoice(amount="4000", date="2026-02-01")

    def tearDown(self) -> None:
        self.h.cleanup()

    def test_query_lists_receipts_in_registration_order(self) -> None:
        self.h.record_receipt(code="RC2", amount="300", date="2026-02-12")
        self.h.record_receipt(code="RC1", amount="100", date="2026-02-10")
        result = self.h.query_invoice()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("发票编号：INV1", result.stdout)
        self.assertIn("合同编号：C001", result.stdout)
        self.assertIn("开票金额：4000", result.stdout)
        self.assertIn("开票日期：2026-02-01", result.stdout)
        self.assertIn("归属：合同", result.stdout)
        self.assertIn("累计收款：400", result.stdout)
        self.assertIn("未收余额：3600", result.stdout)
        self.assertLess(result.stdout.index("RC2"), result.stdout.index("RC1"))
        self.assertIn("收款日期：2026-02-12", result.stdout)
        self.assertIn("收款金额：300", result.stdout)

    def test_query_without_receipts(self) -> None:
        result = self.h.query_invoice()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("累计收款：0", result.stdout)
        self.assertIn("未收余额：4000", result.stdout)

    def test_query_missing_invoice_fails(self) -> None:
        result = self.h.query_invoice(code="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NOPE", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
