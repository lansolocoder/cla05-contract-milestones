"""开票登记、作废、查询展示与对账的端到端 CLI 测试。

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

    # 便捷封装 ----------------------------------------------------------

    def register_contract(self, code="C001", customer="甲方公司",
                          signed="2026-01-15", amount="10000"):
        return self.invoke(
            "register-contract",
            "--code", code, "--customer", customer,
            "--date", signed, "--amount", amount,
        )

    def register_milestone(self, contract="C001", code="MS-01",
                           name="首付款", amount="4000", due="2026-03-01"):
        return self.invoke(
            "register-milestone",
            "--contract", contract, "--code", code, "--name", name,
            "--amount", amount, "--due", due,
        )

    def register_invoice(self, contract="C001", number="INV-001",
                         amount="4000", date="2026-02-01", milestone=None):
        args = [
            "register-invoice",
            "--contract", contract, "--number", number,
            "--amount", amount, "--date", date,
        ]
        if milestone is not None:
            args += ["--milestone", milestone]
        return self.invoke(*args)

    def void_invoice(self, number="INV-001"):
        return self.invoke("void-invoice", "--number", number)

    def reconcile(self, contract="C001", as_of="2026-03-01"):
        return self.invoke(
            "reconcile", "--contract", contract, "--as-of", as_of,
        )

    def query(self, code="C001"):
        return self.invoke("query-contract", "--code", code)


class RegisterInvoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)

    def test_register_success_outputs_number_and_status(self) -> None:
        result = self.cli.register_invoice()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INV-001", result.stdout)
        self.assertIn("已开具", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_duplicate_number_rejected_and_data_unchanged(self) -> None:
        self.assertEqual(self.cli.register_invoice(amount="4000").returncode, 0)
        second = self.cli.register_invoice(number="INV-001", amount="999")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("INV-001", second.stderr)

        result = self.cli.query()
        self.assertEqual(result.returncode, 0, result.stderr)
        # 既有数据未被改动：仍只有一张 4000 的发票。
        self.assertEqual(result.stdout.count("INV-001"), 1)
        self.assertIn("金额：4000", result.stdout)
        self.assertNotIn("999", result.stdout)

    def test_invalid_amounts_fail(self) -> None:
        for bad in ["0", "-1", "1.234", "abc", "1e3", "", " 100"]:
            with self.subTest(bad=bad):
                result = self.cli.register_invoice(
                    number=f"INV-{bad or 'empty'}", amount=bad
                )
                self.assertNotEqual(result.returncode, 0, f"应当拒绝 {bad!r}")
                self.assertIn("金额", result.stderr)
        ok = self.cli.register_invoice(number="INV-MIN", amount="0.01")
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_invalid_date_fails(self) -> None:
        for bad in ["2026-13-01", "2026-02-30", "2026/01/01", "2026-1-1"]:
            with self.subTest(bad=bad):
                result = self.cli.register_invoice(
                    number=f"INV-{bad}", date=bad
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("日期", result.stderr)

    def test_date_before_signed_date_rejected(self) -> None:
        result = self.cli.register_invoice(date="2026-01-14")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("签订日期", result.stderr)
        # 与签订日期同一天可以登记。
        ok = self.cli.register_invoice(number="INV-EQ", date="2026-01-15")
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_unknown_contract_rejected(self) -> None:
        result = self.cli.register_invoice(contract="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NOPE", result.stderr)

    def test_milestone_must_exist_and_belong_to_contract(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(
            self.cli.register_contract(code="C002").returncode, 0
        )

        missing = self.cli.register_invoice(number="INV-A", milestone="MS-99")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("MS-99", missing.stderr)

        foreign = self.cli.register_invoice(number="INV-B", contract="C002",
                                            milestone="MS-01")
        self.assertNotEqual(foreign.returncode, 0)
        self.assertIn("MS-01", foreign.stderr)

        ok = self.cli.register_invoice(number="INV-C", milestone="MS-01")
        self.assertEqual(ok.returncode, 0, ok.stderr)


class VoidInvoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_invoice().returncode, 0)

    def test_void_success(self) -> None:
        result = self.cli.void_invoice()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INV-001", result.stdout)

        query = self.cli.query()
        self.assertIn("已作废", query.stdout)

    def test_double_void_rejected(self) -> None:
        self.assertEqual(self.cli.void_invoice().returncode, 0)
        again = self.cli.void_invoice()
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("INV-001", again.stderr)

    def test_void_unknown_number_rejected(self) -> None:
        result = self.cli.void_invoice(number="INV-99")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INV-99", result.stderr)


class QueryInvoiceSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)

    def test_no_invoices_shows_placeholder(self) -> None:
        result = self.cli.query()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("开票记录：无", result.stdout)

    def test_invoices_listed_in_registration_order(self) -> None:
        self.assertEqual(
            self.cli.register_invoice(
                number="INV-002", amount="100.50", date="2026-02-02"
            ).returncode,
            0,
        )
        self.assertEqual(
            self.cli.register_invoice(
                number="INV-001", amount="200", date="2026-02-01"
            ).returncode,
            0,
        )
        self.assertEqual(self.cli.void_invoice(number="INV-001").returncode, 0)

        result = self.cli.query()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("开票记录：", result.stdout)
        self.assertNotIn("开票记录：无", result.stdout)
        first = result.stdout.index("INV-002")
        second = result.stdout.index("INV-001")
        self.assertLess(first, second, "应按登记先后排列")
        self.assertIn("INV-002", result.stdout)
        self.assertIn("2026-02-02", result.stdout)
        self.assertIn("100.50", result.stdout)
        self.assertIn("已开具", result.stdout)
        self.assertIn("已作废", result.stdout)


class ReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)

    def test_unknown_contract_fails(self) -> None:
        result = self.cli.reconcile(contract="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NOPE", result.stderr)

    def test_invalid_as_of_fails(self) -> None:
        result = self.cli.reconcile(as_of="2026/03/01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("截止日", result.stderr)

    def test_no_milestones_or_all_cleared(self) -> None:
        result = self.cli.reconcile()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("逾期未开票：无", result.stdout)

        # 全额开票后同样无逾期。
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(
            self.cli.register_invoice(milestone="MS-01").returncode, 0
        )
        result = self.cli.reconcile()
        self.assertIn("逾期未开票：无", result.stdout)

    def test_overdue_milestone_reported(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        # 部分开票 1500，差额 2500。
        self.assertEqual(
            self.cli.register_invoice(
                amount="1500", milestone="MS-01"
            ).returncode,
            0,
        )
        # 截止日早于到期日：不逾期。
        early = self.cli.reconcile(as_of="2026-02-28")
        self.assertIn("逾期未开票：无", early.stdout)
        # 截止日等于到期日：逾期。
        result = self.cli.reconcile(as_of="2026-03-01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MS-01 2026-03-01 2500", result.stdout)

    def test_voided_invoice_counts_as_not_issued(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(
            self.cli.register_invoice(milestone="MS-01").returncode, 0
        )
        self.assertEqual(self.cli.void_invoice().returncode, 0)
        result = self.cli.reconcile()
        self.assertIn("MS-01 2026-03-01 4000", result.stdout)

    def test_contract_level_invoice_does_not_count_for_milestone(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        # 归属整份合同的发票不冲抵任何里程碑。
        self.assertEqual(self.cli.register_invoice(amount="4000").returncode, 0)
        result = self.cli.reconcile()
        self.assertIn("MS-01 2026-03-01 4000", result.stdout)

    def test_over_invoice_not_truncated(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        # 开票超过里程碑金额：差额为负，不视为逾期。
        self.assertEqual(
            self.cli.register_invoice(
                amount="9999", milestone="MS-01"
            ).returncode,
            0,
        )
        result = self.cli.reconcile()
        self.assertIn("逾期未开票：无", result.stdout)

    def test_ordering_by_due_date_then_registration(self) -> None:
        self.assertEqual(
            self.cli.register_milestone(
                code="MS-B", amount="100", due="2026-05-01"
            ).returncode,
            0,
        )
        self.assertEqual(
            self.cli.register_milestone(
                code="MS-A", amount="200", due="2026-04-01"
            ).returncode,
            0,
        )
        self.assertEqual(
            self.cli.register_milestone(
                code="MS-C", amount="300", due="2026-05-01"
            ).returncode,
            0,
        )
        result = self.cli.reconcile(as_of="2026-06-01")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(
            lines,
            [
                "MS-A 2026-04-01 200",
                "MS-B 2026-05-01 100",
                "MS-C 2026-05-01 300",
            ],
        )

    def test_milestone_current_amount_includes_effective_changes(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        add = self.cli.invoke(
            "register-change",
            "--contract", "C001", "--code", "CHG-01",
            "--description", "追加", "--delta", "500",
            "--milestone", "MS-01",
        )
        self.assertEqual(add.returncode, 0, add.stderr)
        # 草稿变更不计入。
        result = self.cli.reconcile()
        self.assertIn("MS-01 2026-03-01 4000", result.stdout)
        # 生效后计入里程碑当前金额。
        effect = self.cli.invoke("effect-change", "--code", "CHG-01")
        self.assertEqual(effect.returncode, 0, effect.stderr)
        result = self.cli.reconcile()
        self.assertIn("MS-01 2026-03-01 4500", result.stdout)


if __name__ == "__main__":
    unittest.main()
