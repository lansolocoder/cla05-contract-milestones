"""按里程碑开票与登记收款的端到端 CLI 测试。

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
                           amount="4000", due="2026-03-01"):
        return self.invoke(
            "register-milestone",
            "--contract", contract, "--code", code,
            "--name", name, "--amount", amount, "--due", due,
        )

    def invoice(self, contract="C001", milestone="MS1", code="INV1",
                amount="4000", date="2026-03-01"):
        return self.invoke(
            "invoice",
            "--contract", contract, "--milestone", milestone,
            "--code", code, "--amount", amount, "--date", date,
        )

    def receive(self, invoice="INV1", code="R1", amount="1000",
                date="2026-03-05"):
        return self.invoke(
            "receive",
            "--invoice", invoice, "--code", code,
            "--amount", amount, "--date", date,
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

    def query(self, code="C001"):
        return self.invoke("query-contract", "--code", code)


class InvoiceRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)

    def test_register_success_outputs_code(self) -> None:
        result = self.cli.invoice(amount="3999.50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.strip(), "发票编号：INV1")

    def test_invoice_has_no_draft_state(self) -> None:
        self.assertEqual(self.cli.invoice().returncode, 0)
        query = self.cli.query().stdout
        # 登记即已开具：无收款时状态为未收款，且发票已可查。
        self.assertIn("发票：", query)
        self.assertIn("状态：未收款", query)

    def test_duplicate_code_rejected_globally(self) -> None:
        self.assertEqual(self.cli.invoice().returncode, 0)
        self.cli.register_contract(code="C002", customer="乙", amount="100")
        self.cli.register_milestone(
            contract="C002", code="MS2", amount="100", due="2026-03-01"
        )
        second = self.cli.invoice(
            contract="C002", milestone="MS2", code="INV1",
            amount="100", date="2026-03-01",
        )
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("INV1", second.stderr)
        self.assertIn("发票：无", self.cli.query("C002").stdout)

    def test_unknown_contract_rejected(self) -> None:
        result = self.cli.invoice(contract="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)
        self.assertIn("发票：无", self.cli.query().stdout)

    def test_unknown_milestone_rejected(self) -> None:
        result = self.cli.invoice(milestone="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("里程碑编号不存在", result.stderr)
        self.assertIn("发票：无", self.cli.query().stdout)

    def test_milestone_of_other_contract_rejected(self) -> None:
        self.cli.register_contract(code="C002", customer="乙", amount="100")
        result = self.cli.invoice(contract="C002", milestone="MS1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不属于", result.stderr)
        self.assertIn("发票：无", self.cli.query("C002").stdout)

    def test_non_positive_amount_rejected(self) -> None:
        for bad in ["0", "-1", "0.00"]:
            with self.subTest(bad=bad):
                result = self.cli.invoice(code=f"A-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("金额", result.stderr)

    def test_amount_too_many_decimals_rejected(self) -> None:
        result = self.cli.invoice(code="A1", amount="4000.001")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("金额", result.stderr)

    def test_invalid_date_rejected(self) -> None:
        for bad in ["2026-13-01", "2026/03/01", "20260301", "2026-3-1"]:
            with self.subTest(bad=bad):
                result = self.cli.invoice(code=f"D-{bad}", date=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("日期", result.stderr)

    def test_date_before_due_rejected(self) -> None:
        result = self.cli.invoice(date="2026-02-28")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("到期日", result.stderr)
        self.assertIn("发票：无", self.cli.query().stdout)

    def test_date_equal_to_due_allowed(self) -> None:
        result = self.cli.invoice(date="2026-03-01")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_amount_above_milestone_current_rejected(self) -> None:
        result = self.cli.invoice(amount="4000.01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("超过里程碑当前金额", result.stderr)
        self.assertIn("发票：无", self.cli.query().stdout)

    def test_amount_equal_to_milestone_current_allowed(self) -> None:
        result = self.cli.invoice(amount="4000")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cap_uses_effective_undvoided_changes(self) -> None:
        # 草稿变更不抬高上限。
        self.assertEqual(
            self.cli.register_change(code="D1", delta="500",
                                     milestone="MS1").returncode,
            0,
        )
        self.assertNotEqual(self.cli.invoice(code="I1", amount="4100").returncode, 0)
        # 生效后上限变为 4500。
        self.assertEqual(self.cli.effect("D1").returncode, 0)
        self.assertEqual(
            self.cli.invoice(code="I1", amount="4500").returncode, 0
        )

    def test_failed_invoice_leaves_no_half_write(self) -> None:
        bad = self.cli.invoice(amount="999999")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("发票：无", self.cli.query().stdout)


class ReceiveRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(self.cli.invoice(amount="4000").returncode, 0)

    def test_receive_success_outputs_code(self) -> None:
        result = self.cli.receive(amount="1000.50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.strip(), "收款流水号：R1")

    def test_unknown_invoice_rejected(self) -> None:
        result = self.cli.receive(invoice="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("发票编号不存在", result.stderr)
        self.assertIn("收款：无", self.cli.query().stdout)

    def test_duplicate_receipt_code_rejected_globally(self) -> None:
        self.assertEqual(self.cli.receive(code="DUP", amount="10").returncode, 0)
        # 第二张发票上使用同一流水号仍应拒绝。
        self.cli.register_milestone(code="MS2", amount="100", due="2026-04-01")
        self.assertEqual(
            self.cli.invoice(milestone="MS2", code="INV2", amount="100",
                             date="2026-04-01").returncode,
            0,
        )
        second = self.cli.receive(invoice="INV2", code="DUP", amount="10",
                                  date="2026-04-02")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("DUP", second.stderr)

    def test_non_positive_amount_rejected(self) -> None:
        for bad in ["0", "-1", "0.00"]:
            with self.subTest(bad=bad):
                result = self.cli.receive(code=f"A-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("金额", result.stderr)

    def test_amount_too_many_decimals_rejected(self) -> None:
        result = self.cli.receive(code="A1", amount="1.005")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("金额", result.stderr)

    def test_invalid_date_rejected(self) -> None:
        result = self.cli.receive(date="2026-13-05")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("日期", result.stderr)

    def test_date_before_invoice_date_rejected(self) -> None:
        result = self.cli.receive(date="2026-02-28")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("开票日期", result.stderr)
        # 发票仍为未收款。
        self.assertIn("状态：未收款", self.cli.query().stdout)

    def test_date_equal_to_invoice_date_allowed(self) -> None:
        result = self.cli.receive(date="2026-03-01")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_partial_receipts_allowed(self) -> None:
        self.assertEqual(self.cli.receive(code="R1", amount="1000.5").returncode, 0)
        self.assertEqual(self.cli.receive(code="R2", amount="2000").returncode, 0)
        query = self.cli.query().stdout
        self.assertIn("已收金额：3000.50", query)
        self.assertIn("状态：部分收款", query)

    def test_receipt_equal_to_remaining_completes(self) -> None:
        self.assertEqual(self.cli.receive(code="R1", amount="1000.5").returncode, 0)
        self.assertEqual(self.cli.receive(code="R2", amount="2999.5").returncode, 0)
        query = self.cli.query().stdout
        self.assertIn("已收金额：4000", query)
        self.assertIn("状态：已收齐", query)

    def test_overreceipt_rejected_and_not_added(self) -> None:
        self.assertEqual(self.cli.receive(code="R1", amount="3999.99").returncode, 0)
        bad = self.cli.receive(code="R2", amount="0.02")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("超过发票金额", bad.stderr)
        query = self.cli.query().stdout
        # 本次收款未新增：已收仍为 3999.99，状态仍为部分收款。
        self.assertIn("已收金额：3999.99", query)
        self.assertIn("状态：部分收款", query)
        self.assertIn("- R1", query)
        self.assertNotIn("- R2", query)

    def test_single_exact_receipt_completes(self) -> None:
        self.assertEqual(self.cli.receive(code="R1", amount="4000").returncode, 0)
        query = self.cli.query().stdout
        self.assertIn("状态：已收齐", query)


class InvoiceReceiptQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.cli.register_contract(code="ORD", customer="客户甲",
                                   signed="2026-01-15", amount="10000")
        self.cli.register_milestone(contract="ORD", code="M1", name="首款",
                                    amount="4000", due="2026-03-01")
        self.cli.register_milestone(contract="ORD", code="M2", name="尾款",
                                    amount="6000", due="2026-06-01")

    def test_no_invoices_or_receipts_shows_none(self) -> None:
        result = self.cli.query("ORD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("发票：无", result.stdout)
        self.assertIn("收款：无", result.stdout)

    def test_sections_after_changes_section(self) -> None:
        self.cli.register_change(contract="ORD", code="CH1", description="x",
                                 delta="10")
        self.cli.invoice(contract="ORD", milestone="M1", code="I1",
                         amount="100", date="2026-03-01")
        out = self.cli.query("ORD").stdout
        self.assertLess(out.index("变更单："), out.index("发票："))
        self.assertLess(out.index("发票："), out.index("收款："))

    def test_invoices_listed_in_registration_order(self) -> None:
        self.cli.invoice(contract="ORD", milestone="M2", code="I2",
                         amount="10", date="2026-06-01")
        self.cli.invoice(contract="ORD", milestone="M1", code="I1",
                         amount="20", date="2026-03-01")
        out = self.cli.query("ORD").stdout
        # 按登记先后，而非按日期或里程碑。
        self.assertLess(out.index("- I2"), out.index("- I1"))

    def test_invoice_line_fields(self) -> None:
        self.cli.invoice(contract="ORD", milestone="M1", code="I1",
                         amount="4000", date="2026-03-02")
        self.cli.receive(invoice="I1", code="R1", amount="1000.5",
                         date="2026-03-03")
        out = self.cli.query("ORD").stdout
        self.assertIn(
            "- I1  所属里程碑：M1  开票金额：4000  开票日期：2026-03-02  "
            "已收金额：1000.50  状态：部分收款",
            out,
        )

    def test_receipts_list_contract_wide_in_registration_order(self) -> None:
        self.cli.invoice(contract="ORD", milestone="M1", code="I1",
                         amount="4000", date="2026-03-01")
        self.cli.invoice(contract="ORD", milestone="M2", code="I2",
                         amount="6000", date="2026-06-01")
        self.cli.receive(invoice="I1", code="R1", amount="10",
                         date="2026-03-05")
        self.cli.receive(invoice="I2", code="R2", amount="20",
                         date="2026-06-05")
        self.cli.receive(invoice="I1", code="R3", amount="5",
                         date="2026-03-10")
        out = self.cli.query("ORD").stdout
        # 全局登记先后：R1、R2、R3（不是按发票分组）。
        self.assertLess(out.index("- R1"), out.index("- R2"))
        self.assertLess(out.index("- R2"), out.index("- R3"))
        self.assertIn("所属发票：I1", out)
        self.assertIn("所属发票：I2", out)

    def test_statuses(self) -> None:
        self.cli.invoice(contract="ORD", milestone="M1", code="I1",
                         amount="100", date="2026-03-01")
        self.cli.invoice(contract="ORD", milestone="M1", code="I2",
                         amount="100", date="2026-03-01")
        self.cli.invoice(contract="ORD", milestone="M1", code="I3",
                         amount="100", date="2026-03-01")
        self.cli.receive(invoice="I2", code="R1", amount="50",
                         date="2026-03-02")
        self.cli.receive(invoice="I3", code="R2", amount="100",
                         date="2026-03-02")
        out = self.cli.query("ORD").stdout
        self.assertIn("- I1", out)
        self.assertIn("状态：未收款", out)
        self.assertIn("状态：部分收款", out)
        self.assertIn("状态：已收齐", out)

    def test_query_does_not_modify_data(self) -> None:
        self.cli.invoice(contract="ORD", milestone="M1", code="I1",
                         amount="100", date="2026-03-01")
        before = self.cli.query("ORD").stdout
        after = self.cli.query("ORD").stdout
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
