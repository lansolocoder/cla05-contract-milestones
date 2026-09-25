"""发票登记、作废与对账的端到端 CLI 测试。

每个测试用例使用独立临时数据库（CONTRACT_LEDGER_DB 环境变量
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
                           amount="4000", due="2026-03-01"):
        return self.invoke(
            "register-milestone",
            "--contract", contract, "--code", code,
            "--name", name, "--amount", amount, "--due", due,
        )

    def register_change(self, contract="C001", code="CHG1",
                        description="调价", delta="100", milestone=None):
        args = [
            "register-change",
            "--contract", contract, "--code", code,
            "--description", description, "--delta", delta,
        ]
        if milestone is not None:
            args += ["--milestone", milestone]
        return self.invoke(*args)

    def effect(self, code):
        return self.invoke("effect-change", "--code", code)

    def register_invoice(self, contract="C001", number="INV1", amount="4000",
                         date="2026-03-02", milestone=None):
        args = [
            "register-invoice",
            "--contract", contract, "--number", number,
            "--amount", amount, "--date", date,
        ]
        if milestone is not None:
            args += ["--milestone", milestone]
        return self.invoke(*args)

    def void_invoice(self, number):
        return self.invoke("void-invoice", "--number", number)

    def reconcile(self, contract="C001", as_of="2026-03-01"):
        return self.invoke(
            "reconcile", "--contract", contract, "--as-of", as_of
        )

    def query(self, code="C001"):
        return self.invoke("query-contract", "--code", code)


class RegisterInvoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(
            self.cli.register_milestone().returncode, 0
        )

    def test_contract_level_success_outputs_number_and_status(self) -> None:
        result = self.cli.register_invoice(number="INV0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("发票号：INV0", result.stdout)
        self.assertIn("状态：已开具", result.stdout)

    def test_milestone_level_success(self) -> None:
        result = self.cli.register_invoice(milestone="MS1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_duplicate_number_rejected_globally_and_keeps_data(self) -> None:
        self.assertEqual(self.cli.register_invoice(number="DUP").returncode, 0)
        self.cli.register_contract(code="C002", customer="乙", amount="100")
        second = self.cli.register_invoice(
            contract="C002", number="DUP", amount="1", date="2026-02-01"
        )
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("DUP", second.stderr)
        query = self.cli.query("C002")
        self.assertIn("开票记录：无", query.stdout)

    def test_non_positive_amount_rejected(self) -> None:
        for bad in ["0", "-1", "0.00"]:
            with self.subTest(bad=bad):
                result = self.cli.register_invoice(number=f"A-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("金额", result.stderr)

    def test_amount_too_many_decimals_rejected(self) -> None:
        result = self.cli.register_invoice(number="A1", amount="1.005")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("金额", result.stderr)

    def test_invalid_date_rejected(self) -> None:
        for bad in ["2026-13-01", "2026/03/02", "20260302", "2026-3-2"]:
            with self.subTest(bad=bad):
                result = self.cli.register_invoice(number=f"D-{bad}", date=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("开票日期", result.stderr)

    def test_date_before_signed_date_rejected(self) -> None:
        result = self.cli.register_invoice(date="2026-01-14")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("签订日期", result.stderr)
        self.assertIn("开票记录：无", self.cli.query().stdout)

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
        self.assertIn("开票记录：无", self.cli.query().stdout)

    def test_milestone_of_other_contract_rejected(self) -> None:
        self.cli.register_contract(code="C002", customer="乙", amount="100")
        result = self.cli.register_invoice(
            contract="C002", number="X1", milestone="MS1"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不属于", result.stderr)
        self.assertIn("开票记录：无", self.cli.query("C002").stdout)


class VoidInvoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(
            self.cli.register_invoice(number="INV1").returncode, 0
        )

    def test_void_success_outputs_number(self) -> None:
        result = self.cli.void_invoice("INV1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "发票号：INV1")
        self.assertEqual(result.stderr, "")
        # 状态在查询中体现为“已作废”。
        self.assertIn("INV1", self.cli.query().stdout)
        self.assertIn("已作废", self.cli.query().stdout)

    def test_cannot_void_twice(self) -> None:
        self.assertEqual(self.cli.void_invoice("INV1").returncode, 0)
        again = self.cli.void_invoice("INV1")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("作废", again.stderr)
        # 记录仍可查且状态未变。
        self.assertIn("INV1", self.cli.query().stdout)
        self.assertEqual(self.cli.query().stdout.count("已作废"), 1)

    def test_void_unknown_invoice_fails(self) -> None:
        result = self.cli.void_invoice("MISSING")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)


class InvoiceQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.cli.register_contract(code="ORD", customer="客户甲",
                                   signed="2026-03-08", amount="1000")

    def test_no_invoices_shows_none(self) -> None:
        result = self.cli.query("ORD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("开票记录：无", result.stdout)

    def test_invoices_listed_after_changes_in_registration_order(self) -> None:
        self.cli.register_milestone(contract="ORD", code="M1", name="首款",
                                    amount="300", due="2026-04-01")
        self.cli.register_change(contract="ORD", code="CA",
                                 description="合同级", delta="10")
        self.cli.register_invoice(contract="ORD", number="I1", amount="100.50",
                                  date="2026-04-02")
        self.cli.register_invoice(contract="ORD", number="I2", amount="200",
                                  date="2026-04-03", milestone="M1")
        self.cli.void_invoice("I1")
        result = self.cli.query("ORD")
        self.assertEqual(result.returncode, 0, result.stderr)
        # 开票段在变更单段之后。
        self.assertLess(result.stdout.index("变更单："),
                        result.stdout.index("开票记录："))
        # 按登记先后。
        self.assertLess(result.stdout.index("I1"), result.stdout.index("I2"))
        self.assertIn("开票日期：2026-04-02  金额：100.50  状态：已作废",
                      result.stdout)
        self.assertIn("开票日期：2026-04-03  金额：200  状态：已开具",
                      result.stdout)

    def test_query_does_not_modify_data(self) -> None:
        self.cli.register_invoice(contract="ORD", number="I1", amount="100",
                                  date="2026-04-02")
        before = self.cli.query("ORD").stdout
        after = self.cli.query("ORD").stdout
        self.assertEqual(before, after)


class ReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(
            self.cli.register_contract(amount="10000").returncode, 0
        )

    def test_unknown_contract_fails(self) -> None:
        result = self.cli.reconcile(contract="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_invalid_as_of_rejected(self) -> None:
        result = self.cli.reconcile(as_of="2026-02-30")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("截止日", result.stderr)

    def test_no_milestones_outputs_none(self) -> None:
        result = self.cli.reconcile(as_of="2026-12-31")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "逾期未开票：无")

    def test_not_due_yet_outputs_none(self) -> None:
        self.cli.register_milestone(amount="4000", due="2026-03-01")
        result = self.cli.reconcile(as_of="2026-02-28")
        self.assertEqual(result.stdout.strip(), "逾期未开票：无")

    def test_due_with_gap_listed_on_due_date(self) -> None:
        # 截止日等于到期日即视为逾期。
        self.cli.register_milestone(code="M1", amount="4000", due="2026-03-01")
        result = self.cli.reconcile(as_of="2026-03-01")
        self.assertEqual(result.stdout, "M1 2026-03-01 4000\n")

    def test_gap_uses_milestone_current_amount(self) -> None:
        self.cli.register_milestone(code="M1", amount="4000", due="2026-03-01")
        # 已生效未作废里程碑变更 +500 计入当前金额；草稿不计。
        self.assertEqual(
            self.cli.register_change(code="E1", delta="500",
                                     milestone="M1").returncode, 0
        )
        self.assertEqual(self.cli.effect("E1").returncode, 0)
        self.assertEqual(
            self.cli.register_change(code="D1", delta="999",
                                     milestone="M1").returncode, 0
        )
        # 已开 4200，差额应为 4500 - 4200 = 300。
        self.assertEqual(
            self.cli.register_invoice(number="I1", amount="4200",
                                      date="2026-03-02",
                                      milestone="M1").returncode, 0
        )
        result = self.cli.reconcile(as_of="2026-03-02")
        self.assertEqual(result.stdout, "M1 2026-03-01 300\n")

    def test_over_invoicing_not_truncated_and_not_listed(self) -> None:
        self.cli.register_milestone(code="M1", amount="4000", due="2026-03-01")
        self.cli.register_invoice(number="I1", amount="5000",
                                  date="2026-03-02", milestone="M1")
        result = self.cli.reconcile(as_of="2026-03-02")
        self.assertEqual(result.stdout.strip(), "逾期未开票：无")

    def test_fully_invoiced_not_listed(self) -> None:
        self.cli.register_milestone(code="M1", amount="4000", due="2026-03-01")
        self.cli.register_invoice(number="I1", amount="4000",
                                  date="2026-03-01", milestone="M1")
        result = self.cli.reconcile(as_of="2026-03-01")
        self.assertEqual(result.stdout.strip(), "逾期未开票：无")

    def test_voided_invoice_counted_back_as_gap(self) -> None:
        self.cli.register_milestone(code="M1", amount="4000", due="2026-03-01")
        self.cli.register_invoice(number="I1", amount="4000",
                                  date="2026-03-01", milestone="M1")
        self.assertEqual(self.cli.void_invoice("I1").returncode, 0)
        result = self.cli.reconcile(as_of="2026-03-01")
        self.assertEqual(result.stdout, "M1 2026-03-01 4000\n")

    def test_contract_level_invoice_not_counted_to_milestone(self) -> None:
        self.cli.register_milestone(code="M1", amount="4000", due="2026-03-01")
        # 合同级发票（省略 --milestone）不抵扣任何里程碑。
        self.cli.register_invoice(number="I0", amount="4000",
                                  date="2026-03-01")
        result = self.cli.reconcile(as_of="2026-03-01")
        self.assertEqual(result.stdout, "M1 2026-03-01 4000\n")

    def test_sorted_by_due_date_then_registration_order(self) -> None:
        self.cli.register_milestone(code="LATE", amount="100",
                                    due="2026-05-01")
        self.cli.register_milestone(code="SAME2", amount="200",
                                    due="2026-04-01")
        self.cli.register_milestone(code="SAME1", amount="300",
                                    due="2026-04-01")
        result = self.cli.reconcile(as_of="2026-05-01")
        lines = result.stdout.splitlines()
        self.assertEqual(
            lines,
            [
                "SAME2 2026-04-01 200",
                "SAME1 2026-04-01 300",
                "LATE 2026-05-01 100",
            ],
        )

    def test_reconcile_does_not_modify_data(self) -> None:
        self.cli.register_milestone(code="M1", amount="4000", due="2026-03-01")
        before = self.cli.query().stdout
        self.cli.reconcile(as_of="2026-12-31")
        self.cli.reconcile(as_of="2026-01-01")
        after = self.cli.query().stdout
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
