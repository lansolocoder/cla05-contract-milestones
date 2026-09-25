"""按合同登记分配收款的端到端 CLI 测试。

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

    def allocate(self, *items, contract="C001", code="RCV-A1",
                 date="2026-03-10"):
        arguments = [
            "allocate-receipt",
            "--contract", contract, "--code", code, "--date", date,
        ]
        for invoice_code, amount in items:
            arguments += ["--alloc", invoice_code, amount]
        return self.invoke(*arguments)

    def query(self, code="C001"):
        return self.invoke("query-contract", "--code", code)


class AllocateReceiptTests(unittest.TestCase):
    """两张发票（INV01/INV02 各 4000）下的分配收款。"""

    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(
            self.cli.register_milestone(
                code="MS02", name="尾款", amount="6000", due="2026-03-01"
            ).returncode,
            0,
        )
        self.assertEqual(self.cli.invoice(code="INV01", amount="4000").returncode, 0)
        self.assertEqual(
            self.cli.invoice(
                milestone="MS02", code="INV02", amount="4000", date="2026-03-05"
            ).returncode,
            0,
        )

    def test_allocate_success_outputs_code(self) -> None:
        result = self.cli.allocate(("INV01", "1500"), ("INV02", "2500.50"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "收款流水号：RCV-A1")
        self.assertEqual(result.stderr, "")

    def test_allocate_updates_received_and_status(self) -> None:
        self.assertEqual(
            self.cli.allocate(("INV01", "4000"), ("INV02", "1000")).returncode, 0
        )
        out = self.cli.query().stdout
        # INV01 收齐，INV02 部分收款；收款金额为各分配之和。
        self.assertIn("INV01", out)
        self.assertIn("状态：已收齐", out)
        self.assertIn("状态：部分收款", out)
        self.assertIn("金额：5000", out)
        self.assertIn("分配：INV01 4000 INV02 1000", out)

    def test_allocate_item_order_preserved_in_query(self) -> None:
        self.assertEqual(
            self.cli.allocate(("INV02", "2000.5"), ("INV01", "1000")).returncode, 0
        )
        out = self.cli.query().stdout
        self.assertIn("分配：INV02 2000.50 INV01 1000", out)

    def test_normal_receipt_shows_whole_allocation(self) -> None:
        self.assertEqual(self.cli.receive().returncode, 0)
        out = self.cli.query().stdout
        self.assertIn("分配：整笔归 INV01", out)

    def test_receipt_lines_keep_registration_order(self) -> None:
        self.assertEqual(self.cli.receive(code="RCV01").returncode, 0)
        self.assertEqual(
            self.cli.allocate(("INV02", "500"), code="RCV-A1").returncode, 0
        )
        out = self.cli.query().stdout
        self.assertLess(out.index("RCV01"), out.index("RCV-A1"))

    def test_zero_items_rejected(self) -> None:
        result = self.cli.allocate()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(result.stderr, "")
        self.assertEqual(result.stdout, "")
        self.assertIn("收款：无", self.cli.query().stdout)

    def test_duplicate_receipt_code_rejected(self) -> None:
        self.assertEqual(
            self.cli.allocate(("INV01", "100"), code="RCV-A1").returncode, 0
        )
        second = self.cli.allocate(("INV02", "100"), code="RCV-A1")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("RCV-A1", second.stderr)
        self.assertEqual(self.cli.query().stdout.count("RCV-A1"), 1)

    def test_code_conflicts_with_normal_receipt(self) -> None:
        self.assertEqual(self.cli.receive(code="RCV01").returncode, 0)
        result = self.cli.allocate(("INV01", "100"), code="RCV01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RCV01", result.stderr)

    def test_unknown_contract_rejected(self) -> None:
        result = self.cli.allocate(("INV01", "100"), contract="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NOPE", result.stderr)

    def test_unknown_invoice_rejected(self) -> None:
        result = self.cli.allocate(("INV01", "100"), ("GHOST", "50"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GHOST", result.stderr)
        # 整体失败：INV01 的分配也不落库。
        self.assertIn("收款：无", self.cli.query().stdout)

    def test_invoice_of_other_contract_rejected(self) -> None:
        self.assertEqual(
            self.cli.register_contract(code="C002", amount="5000").returncode, 0
        )
        self.assertEqual(
            self.cli.register_milestone(
                contract="C002", code="MS-X", name="其他", amount="1000"
            ).returncode,
            0,
        )
        self.assertEqual(
            self.cli.invoice(
                contract="C002", milestone="MS-X", code="INV-X", amount="1000"
            ).returncode,
            0,
        )
        result = self.cli.allocate(("INV01", "100"), ("INV-X", "100"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INV-X", result.stderr)
        self.assertIn("收款：无", self.cli.query().stdout)

    def test_duplicate_invoice_in_same_operation_rejected(self) -> None:
        result = self.cli.allocate(("INV01", "100"), ("INV01", "200"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INV01", result.stderr)
        self.assertIn("收款：无", self.cli.query().stdout)

    def test_date_must_not_precede_any_invoice_date(self) -> None:
        # INV02 开票日期为 2026-03-05，收款日 2026-03-04 早于它。
        result = self.cli.allocate(
            ("INV01", "100"), ("INV02", "100"), date="2026-03-04"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("2026-03-05", result.stderr)
        self.assertIn("收款：无", self.cli.query().stdout)

    def test_invalid_allocation_amounts_rejected(self) -> None:
        for amount in ["0", "-5", "1.234", "abc"]:
            with self.subTest(amount=amount):
                result = self.cli.allocate(("INV01", amount))
                self.assertNotEqual(result.returncode, 0)
        self.assertIn("收款：无", self.cli.query().stdout)

    def test_cumulative_received_must_not_exceed_invoice_amount(self) -> None:
        self.assertEqual(self.cli.receive(code="RCV01", amount="3000").returncode, 0)
        result = self.cli.allocate(("INV01", "1000.01"), ("INV02", "100"))
        self.assertNotEqual(result.returncode, 0)
        out = self.cli.query().stdout
        # 整体拒绝：INV02 的分配也不落库，既有收款不变。
        self.assertNotIn("RCV-A1", out)
        self.assertIn("已收金额：3000", out)
        self.assertIn("状态：未收款", out)  # INV02 仍未收款

    def test_allocate_exact_full_amount_ok(self) -> None:
        self.assertEqual(
            self.cli.allocate(("INV01", "4000"), ("INV02", "4000")).returncode, 0
        )
        out = self.cli.query().stdout
        self.assertEqual(out.count("状态：已收齐"), 2)

    def test_query_does_not_modify_data(self) -> None:
        self.assertEqual(
            self.cli.allocate(("INV01", "100"), ("INV02", "200")).returncode, 0
        )
        before = self.cli.query().stdout
        after = self.cli.query().stdout
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
