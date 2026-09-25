"""开票登记与收款匹配的端到端 CLI 测试。

每个测试用例使用独立临时数据库（通过 CONTRACT_LEDGER_DB 环境变量
指定），测试结束后清理，不污染项目内默认数据库。
"""

from datetime import date
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InvoiceHarness:
    """在临时数据库上调用 python3 -m contract_ledger。"""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ledger-inv-test-")
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

    def register_milestone(self, contract="C001", code="MS01", title="首付款",
                           amount="3000", due="2026-03-01"):
        return self.invoke(
            "register-milestone",
            "--contract", contract, "--code", code,
            "--title", title, "--amount", amount, "--due", due,
        )

    def confirm_milestone(self, code="MS01"):
        return self.invoke("confirm-milestone", "--code", code)

    def register_invoice(self, milestone="MS01", code="INV01",
                         amount="1000", inv_date=None):
        args = ["register-invoice", "--milestone", milestone,
                "--code", code, "--amount", amount]
        if inv_date is not None:
            args += ["--date", inv_date]
        return self.invoke(*args)

    def register_payment(self, code="PAY01", invoice=None, contract=None,
                         amount="500", pay_date=None):
        args = ["register-payment", "--code", code, "--amount", amount]
        if invoice is not None:
            args += ["--invoice", invoice]
        if contract is not None:
            args += ["--contract", contract]
        if pay_date is not None:
            args += ["--date", pay_date]
        return self.invoke(*args)

    def query_contract(self, code="C001"):
        return self.invoke("query-contract", "--code", code)

    def query_invoice(self, code="INV01"):
        return self.invoke("query-invoice", "--code", code)

    def query_payment(self, code="PAY01"):
        return self.invoke("query-payment", "--code", code)


class InvoiceRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = InvoiceHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)

    def test_register_on_confirmed_milestone(self) -> None:
        self.cli.confirm_milestone()
        result = self.cli.register_invoice(inv_date="2026-04-01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INV01", result.stdout)
        query = self.cli.query_invoice()
        self.assertIn("里程碑编号：MS01", query.stdout)
        self.assertIn("金额：1000.00", query.stdout)
        self.assertIn("日期：2026-04-01", query.stdout)
        self.assertIn("状态：已开票", query.stdout)

    def test_default_date_is_registration_day(self) -> None:
        self.cli.confirm_milestone()
        result = self.cli.register_invoice()
        self.assertEqual(result.returncode, 0, result.stderr)
        query = self.cli.query_invoice()
        self.assertIn(f"日期：{date.today().isoformat()}", query.stdout)

    def test_pending_milestone_rejected(self) -> None:
        result = self.cli.register_invoice()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("已确认", result.stderr)
        # 不留下任何发票记录。
        self.assertIn("开票：无", self.cli.query_contract().stdout)

    def test_voided_milestone_rejected(self) -> None:
        self.cli.confirm_milestone()
        self.cli.invoke("cancel-milestone", "--code", "MS01")
        result = self.cli.register_invoice()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("开票：无", self.cli.query_contract().stdout)

    def test_unknown_milestone_rejected(self) -> None:
        result = self.cli.register_invoice(milestone="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_duplicate_invoice_code_fails(self) -> None:
        self.cli.confirm_milestone()
        self.assertEqual(self.cli.register_invoice().returncode, 0)
        again = self.cli.register_invoice(amount="1")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("INV01", again.stderr)

    def test_invalid_amounts_fail_before_write(self) -> None:
        self.cli.confirm_milestone()
        for bad in ["0", "-1", "-0.01", "1.234", "abc", "1e3", "", " 100"]:
            with self.subTest(bad=bad):
                result = self.cli.register_invoice(
                    code=f"E-{bad or 'empty'}", amount=bad
                )
                self.assertNotEqual(result.returncode, 0, f"应当拒绝 {bad!r}")
                self.assertIn("金额", result.stderr)
        ok = self.cli.register_invoice(code="INV-MIN", amount="0.01")
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_invalid_date_rejected(self) -> None:
        self.cli.confirm_milestone()
        for bad in ["2026-13-01", "2026/04/01", "2026-4-1"]:
            with self.subTest(bad=bad):
                result = self.cli.register_invoice(inv_date=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("日期", result.stderr)


class PaymentRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = InvoiceHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(self.cli.confirm_milestone().returncode, 0)
        self.assertEqual(self.cli.register_invoice(amount="1000").returncode, 0)

    def test_payment_on_invoice(self) -> None:
        result = self.cli.register_payment(invoice="INV01", pay_date="2026-05-01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("匹配目标：INV01", result.stdout)
        query = self.cli.query_payment()
        self.assertIn("金额：500.00", query.stdout)
        self.assertIn("日期：2026-05-01", query.stdout)
        self.assertIn("匹配目标：INV01", query.stdout)

    def test_payment_on_contract(self) -> None:
        result = self.cli.register_payment(contract="C001", amount="200")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("匹配目标：合同：C001", result.stdout)

    def test_both_targets_is_illegal(self) -> None:
        result = self.cli.register_payment(invoice="INV01", contract="C001")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("二选一", result.stderr)
        # 不写入任何记录。
        self.assertIn("收款：无", self.cli.query_contract().stdout)

    def test_no_target_is_illegal(self) -> None:
        result = self.cli.register_payment()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("收款：无", self.cli.query_contract().stdout)

    def test_unknown_invoice_or_contract_rejected(self) -> None:
        r1 = self.cli.register_payment(code="P1", invoice="NOPE")
        self.assertNotEqual(r1.returncode, 0)
        self.assertIn("不存在", r1.stderr)
        r2 = self.cli.register_payment(code="P2", contract="NOPE")
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("不存在", r2.stderr)

    def test_cumulative_over_invoice_amount_rejected_atomically(self) -> None:
        self.assertEqual(
            self.cli.register_payment(code="P1", invoice="INV01", amount="600")
            .returncode, 0
        )
        # 600 + 500 > 1000：整笔拒绝。
        over = self.cli.register_payment(code="P2", invoice="INV01", amount="500")
        self.assertNotEqual(over.returncode, 0)
        self.assertIn("超过", over.stderr)
        # 原记录不变：只有 P1 一笔，状态仍是部分收款。
        query = self.cli.query_invoice()
        self.assertIn("P1", query.stdout)
        self.assertNotIn("P2", query.stdout)
        self.assertIn("状态：部分收款", query.stdout)
        # 恰好收齐仍允许。
        exact = self.cli.register_payment(code="P3", invoice="INV01", amount="400")
        self.assertEqual(exact.returncode, 0, exact.stderr)
        self.assertIn("状态：已收齐", self.cli.query_invoice().stdout)
        # 收齐后再收一分钱也拒绝。
        extra = self.cli.register_payment(code="P4", invoice="INV01", amount="0.01")
        self.assertNotEqual(extra.returncode, 0)

    def test_duplicate_payment_code_is_idempotent(self) -> None:
        first = self.cli.register_payment(invoice="INV01", pay_date="2026-05-01")
        self.assertEqual(first.returncode, 0, first.stderr)
        # 同一编号再次登记（金额、日期不同）：返回首次输出，不产生第二笔。
        second = self.cli.register_payment(invoice="INV01", amount="999",
                                           pay_date="2026-06-01")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        query = self.cli.query_invoice()
        self.assertEqual(query.stdout.count("- PAY01"), 1)
        self.assertIn("金额：500.00", query.stdout)
        self.assertNotIn("999", query.stdout)

    def test_invalid_amounts_fail(self) -> None:
        for bad in ["0", "-1", "1.234", "abc"]:
            with self.subTest(bad=bad):
                result = self.cli.register_payment(
                    code=f"E-{bad}", invoice="INV01", amount=bad
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("金额", result.stderr)


class QueryContractInvoiceSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = InvoiceHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract(amount="10000").returncode, 0)

    def test_empty_sections_show_zero_totals(self) -> None:
        query = self.cli.query_contract()
        self.assertIn("开票：无", query.stdout)
        self.assertIn("收款：无", query.stdout)
        self.assertIn("开票合计：0.00", query.stdout)
        self.assertIn("收款合计：0.00", query.stdout)
        self.assertIn("未匹配差额：0.00", query.stdout)

    def test_invoice_statuses_and_totals(self) -> None:
        cli = self.cli
        cli.register_milestone(code="MS1", amount="3000")
        cli.register_milestone(code="MS2", amount="2000")
        cli.register_milestone(code="MS3", amount="1000")
        for ms in ("MS1", "MS2", "MS3"):
            cli.confirm_milestone(ms)
        cli.register_invoice(milestone="MS1", code="INV1", amount="3000")
        cli.register_invoice(milestone="MS2", code="INV2", amount="2000")
        cli.register_invoice(milestone="MS3", code="INV3", amount="1000")
        cli.register_payment(code="P1", invoice="INV1", amount="3000")   # 已收齐
        cli.register_payment(code="P2", invoice="INV2", amount="500")    # 部分收款
        cli.register_payment(code="P3", contract="C001", amount="800")   # 按合同
        query = cli.query_contract()
        out = query.stdout
        self.assertEqual(query.returncode, 0, query.stderr)
        # 开票段：按登记先后，状态字面值精确。
        self.assertLess(out.index("INV1"), out.index("INV2"))
        self.assertLess(out.index("INV2"), out.index("INV3"))
        lines = {ln.split()[1]: ln for ln in out.splitlines()
                 if ln.strip().startswith("- INV")}
        self.assertIn("状态：已收齐", lines["INV1"])
        self.assertIn("状态：部分收款", lines["INV2"])
        self.assertIn("状态：已开票", lines["INV3"])
        self.assertIn("里程碑：MS1", lines["INV1"])
        self.assertIn("金额：3000.00", lines["INV1"])
        # 收款段：按登记先后，匹配目标分别为发票编号与“合同：C001”。
        self.assertLess(out.index("P1"), out.index("P2"))
        self.assertLess(out.index("P2"), out.index("P3"))
        pay_lines = {ln.split()[1]: ln for ln in out.splitlines()
                     if ln.strip().startswith("- P")}
        self.assertIn("匹配目标：INV1", pay_lines["P1"])
        self.assertIn("匹配目标：合同：C001", pay_lines["P3"])
        self.assertIn("金额：800.00", pay_lines["P3"])
        # 合计：开票 6000，收款 4300，差额 1700。
        self.assertIn("开票合计：6000.00", out)
        self.assertIn("收款合计：4300.00", out)
        self.assertIn("未匹配差额：1700.00", out)

    def test_negative_difference_when_overpaid_by_contract(self) -> None:
        cli = self.cli
        cli.register_milestone(code="MS1", amount="1000")
        cli.confirm_milestone("MS1")
        cli.register_invoice(milestone="MS1", code="INV1", amount="1000")
        cli.register_payment(code="P1", invoice="INV1", amount="1000")
        cli.register_payment(code="P2", contract="C001", amount="1500")
        out = cli.query_contract().stdout
        self.assertIn("开票合计：1000.00", out)
        self.assertIn("收款合计：2500.00", out)
        self.assertIn("未匹配差额：-1500.00", out)


class QueryInvoicePaymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = InvoiceHarness()
        self.addCleanup(self.cli.cleanup)

    def test_query_invoice_lists_payments_in_order(self) -> None:
        cli = self.cli
        cli.register_contract()
        cli.register_milestone()
        cli.confirm_milestone()
        cli.register_invoice(amount="1000", inv_date="2026-04-01")
        cli.register_payment(code="P1", invoice="INV01", amount="300",
                             pay_date="2026-05-01")
        cli.register_payment(code="P2", invoice="INV01", amount="200",
                             pay_date="2026-05-02")
        result = cli.query_invoice()
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("发票编号：INV01", out)
        self.assertIn("里程碑编号：MS01", out)
        self.assertIn("金额：1000.00", out)
        self.assertIn("日期：2026-04-01", out)
        self.assertIn("状态：部分收款", out)
        self.assertLess(out.index("P1"), out.index("P2"))
        self.assertIn("金额：300.00", out)
        self.assertIn("日期：2026-05-02", out)

    def test_query_invoice_without_payments(self) -> None:
        cli = self.cli
        cli.register_contract()
        cli.register_milestone()
        cli.confirm_milestone()
        cli.register_invoice()
        result = cli.query_invoice()
        self.assertIn("状态：已开票", result.stdout)
        self.assertIn("收款：无", result.stdout)

    def test_query_unknown_invoice_fails_on_stderr(self) -> None:
        result = self.cli.query_invoice("NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_query_unknown_payment_fails_on_stderr(self) -> None:
        result = self.cli.query_payment("NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
