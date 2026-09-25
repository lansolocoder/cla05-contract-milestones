"""按里程碑开票与登记收款的端到端 CLI 测试。

每个测试用例使用独立临时数据库（通过 CONTRACT_LEDGER_DB 环境变量
指定），测试结束后清理，不污染项目内默认数据库。
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
    """在临时数据库上调用 python3 -m contract_ledger。"""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ledger-test-")
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

    # 便捷封装 ----------------------------------------------------------

    def register_contract(self, code="C001", customer="甲方公司",
                          signed="2026-01-15", amount="10000"):
        return self.invoke(
            "register-contract",
            "--code", code, "--customer", customer,
            "--date", signed, "--amount", amount,
        )

    def register_milestone(self, contract="C001", code="MS01",
                           name="首付款", amount="4000", due="2026-03-01"):
        return self.invoke(
            "register-milestone",
            "--contract", contract, "--code", code,
            "--name", name, "--amount", amount, "--due", due,
        )

    def register_change(self, contract="C001", code="CHG01",
                        description="追加需求", delta="2000",
                        milestone=None):
        arguments = [
            "register-change",
            "--contract", contract, "--code", code,
            "--description", description, "--delta", delta,
        ]
        if milestone is not None:
            arguments += ["--milestone", milestone]
        return self.invoke(*arguments)

    def effect_change(self, code="CHG01"):
        return self.invoke("effect-change", "--code", code)

    def invoice(self, contract="C001", milestone="MS01", code="INV01",
                amount="4000", date="2026-03-01"):
        return self.invoke(
            "invoice",
            "--contract", contract, "--milestone", milestone,
            "--code", code, "--amount", amount, "--date", date,
        )

    def receive(self, invoice="INV01", code="RCV01",
                amount="1000", date="2026-03-05"):
        return self.invoke(
            "receive",
            "--invoice", invoice, "--code", code,
            "--amount", amount, "--date", date,
        )

    def query(self, code="C001"):
        return self.invoke("query-contract", "--code", code)


class InvoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)

    def test_invoice_success_outputs_code(self) -> None:
        result = self.cli.invoice()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "发票编号：INV01")
        self.assertEqual(result.stderr, "")

    def test_duplicate_invoice_code_fails_and_keeps_data(self) -> None:
        self.assertEqual(self.cli.invoice().returncode, 0)
        second = self.cli.invoice(code="INV01", amount="100")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("INV01", second.stderr)
        query = self.cli.query()
        self.assertEqual(query.stdout.count("INV01"), 1)

    def test_invoice_unknown_contract_fails(self) -> None:
        result = self.cli.invoice(contract="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NOPE", result.stderr)

    def test_invoice_unknown_milestone_fails(self) -> None:
        result = self.cli.invoice(milestone="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NOPE", result.stderr)

    def test_invoice_milestone_of_other_contract_fails(self) -> None:
        self.assertEqual(
            self.cli.register_contract(code="C002", amount="5000").returncode, 0
        )
        result = self.cli.invoice(contract="C002", milestone="MS01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MS01", result.stderr)
        self.assertIn("C002", result.stderr)

    def test_invoice_amount_must_not_exceed_milestone_current(self) -> None:
        result = self.cli.invoice(amount="4000.01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("4000", result.stderr)
        query = self.cli.query()
        self.assertIn("发票：无", query.stdout)

    def test_invoice_amount_uses_effective_changes(self) -> None:
        # 生效变更提高里程碑当前金额后即可开更大金额的发票。
        self.assertEqual(
            self.cli.register_change(delta="500", milestone="MS01").returncode, 0
        )
        self.assertEqual(self.cli.effect_change().returncode, 0)
        result = self.cli.invoice(amount="4500")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invoice_amount_rejects_draft_change(self) -> None:
        # 草稿变更不计入里程碑当前金额。
        self.assertEqual(
            self.cli.register_change(delta="500", milestone="MS01").returncode, 0
        )
        result = self.cli.invoice(amount="4500")
        self.assertNotEqual(result.returncode, 0)

    def test_invoice_date_must_not_precede_due_date(self) -> None:
        result = self.cli.invoice(date="2026-02-28")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("2026-03-01", result.stderr)

    def test_invoice_rejects_bad_amount_and_date(self) -> None:
        for kwargs in [{"amount": "0"}, {"amount": "-1"},
                       {"amount": "1.234"}, {"amount": "abc"},
                       {"date": "2026-3-1"}, {"date": "not-a-date"}]:
            with self.subTest(kwargs=kwargs):
                result = self.cli.invoice(**kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotEqual(result.stderr, "")


class ReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(self.cli.invoice().returncode, 0)

    def test_receive_success_outputs_code(self) -> None:
        result = self.cli.receive()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "收款流水号：RCV01")
        self.assertEqual(result.stderr, "")

    def test_receive_unknown_invoice_fails(self) -> None:
        result = self.cli.receive(invoice="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NOPE", result.stderr)

    def test_duplicate_receipt_code_fails(self) -> None:
        self.assertEqual(self.cli.receive().returncode, 0)
        second = self.cli.receive(code="RCV01", amount="500")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("RCV01", second.stderr)
        query = self.cli.query()
        self.assertEqual(query.stdout.count("RCV01"), 1)

    def test_receive_date_must_not_precede_invoice_date(self) -> None:
        result = self.cli.receive(date="2026-02-28")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("2026-03-01", result.stderr)

    def test_partial_receipts_up_to_invoice_amount(self) -> None:
        self.assertEqual(self.cli.receive(code="R1", amount="1500").returncode, 0)
        self.assertEqual(self.cli.receive(code="R2", amount="2500").returncode, 0)
        query = self.cli.query()
        self.assertIn("已收齐", query.stdout)

    def test_receipt_total_must_not_exceed_invoice_amount(self) -> None:
        self.assertEqual(self.cli.receive(code="R1", amount="3000").returncode, 0)
        result = self.cli.receive(code="R2", amount="1000.01")
        self.assertNotEqual(result.returncode, 0)
        query = self.cli.query()
        self.assertNotIn("R2", query.stdout)
        self.assertIn("部分收款", query.stdout)

    def test_receive_rejects_bad_amount(self) -> None:
        for amount in ["0", "-5", "1.234", "abc"]:
            with self.subTest(amount=amount):
                result = self.cli.receive(amount=amount)
                self.assertNotEqual(result.returncode, 0)


class QueryInvoiceReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)

    def test_query_without_invoices_or_receipts(self) -> None:
        result = self.cli.query()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("发票：无", result.stdout)
        self.assertIn("收款：无", result.stdout)

    def test_query_lists_invoices_and_receipts_in_order(self) -> None:
        self.assertEqual(self.cli.invoice(code="INV01", amount="4000").returncode, 0)
        self.assertEqual(
            self.cli.register_milestone(
                code="MS02", name="尾款", amount="6000", due="2026-06-01"
            ).returncode,
            0,
        )
        self.assertEqual(
            self.cli.invoice(
                milestone="MS02", code="INV02", amount="6000", date="2026-06-01"
            ).returncode,
            0,
        )
        self.assertEqual(
            self.cli.receive(invoice="INV01", code="RCV01", amount="1000").returncode,
            0,
        )
        self.assertEqual(
            self.cli.receive(
                invoice="INV02", code="RCV02", amount="6000", date="2026-06-02"
            ).returncode,
            0,
        )
        result = self.cli.query()
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        # 发票段按登记先后，含里程碑、金额、日期、已收与状态。
        self.assertLess(out.index("INV01"), out.index("INV02"))
        self.assertIn("状态：部分收款", out)
        self.assertIn("状态：已收齐", out)
        self.assertIn("已收金额：1000", out)
        # 收款段按登记先后，含所属发票、金额与收款日。
        self.assertLess(out.index("RCV01"), out.index("RCV02"))
        self.assertIn("收款日：2026-06-02", out)

    def test_query_status_unpaid(self) -> None:
        self.assertEqual(self.cli.invoice().returncode, 0)
        result = self.cli.query()
        self.assertIn("状态：未收款", result.stdout)
        self.assertIn("已收金额：0", result.stdout)

    def test_amount_formatting_strips_trailing_zeros(self) -> None:
        self.assertEqual(self.cli.invoice(amount="1234.50").returncode, 0)
        result = self.cli.query()
        self.assertIn("开票金额：1234.5", result.stdout)


if __name__ == "__main__":
    unittest.main()
