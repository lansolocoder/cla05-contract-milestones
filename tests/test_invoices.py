"""开票登记与收款匹配的端到端 CLI 测试。

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


class BillingHarness:
    """在临时数据库上调用 python3 -m contract_ledger。"""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ledger-bill-test-")
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
            "register-contract", "--code", code, "--customer", "甲方公司",
            "--date", "2026-01-15", "--amount", amount,
        )

    def register_milestone(self, code="MS01", contract="C001",
                           amount="3000", due="2026-03-01"):
        return self.invoke(
            "register-milestone", "--contract", contract, "--code", code,
            "--title", "首付款", "--amount", amount, "--due", due,
        )

    def confirm(self, code="MS01"):
        return self.invoke("confirm-milestone", "--code", code)

    def invoice(self, milestone="MS01", code="INV01", amount="1000", date=None):
        args = ["register-invoice", "--milestone", milestone,
                "--code", code, "--amount", amount]
        if date is not None:
            args += ["--date", date]
        return self.invoke(*args)

    def pay(self, code, amount, invoice=None, contract=None, date=None):
        args = ["register-payment", "--code", code, "--amount", amount]
        if invoice is not None:
            args += ["--invoice", invoice]
        if contract is not None:
            args += ["--contract", contract]
        if date is not None:
            args += ["--date", date]
        return self.invoke(*args)

    def query_contract(self, code="C001"):
        return self.invoke("query-contract", "--code", code)


class RegisterInvoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = BillingHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)

    def test_invoice_requires_confirmed_milestone(self) -> None:
        # 待确认里程碑不能开票。
        result = self.cli.invoice()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("已确认", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_invoice_on_voided_milestone_rejected(self) -> None:
        self.cli.invoke("cancel-milestone", "--code", "MS01")
        result = self.cli.invoice()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("已确认", result.stderr)

    def test_invoice_on_unknown_milestone_rejected(self) -> None:
        result = self.cli.invoice(milestone="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_invoice_success_uses_given_date(self) -> None:
        self.assertEqual(self.cli.confirm().returncode, 0)
        result = self.cli.invoice(amount="1000.50", date="2026-04-01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INV01", result.stdout)
        self.assertIn("1000.50", result.stdout)
        self.assertIn("2026-04-01", result.stdout)

    def test_invoice_without_date_uses_today(self) -> None:
        self.cli.confirm()
        result = self.cli.invoice()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2026-09-25", result.stdout)

    def test_duplicate_invoice_code_rejected_and_unchanged(self) -> None:
        self.cli.confirm()
        self.assertEqual(self.cli.invoice(amount="1000").returncode, 0)
        dup = self.cli.invoice(amount="2000", date="2026-05-01")
        self.assertNotEqual(dup.returncode, 0)
        self.assertIn("INV01", dup.stderr)
        # 既有发票金额与日期未被改动。
        query = self.invoke_query_invoice("INV01")
        self.assertIn("金额：1000.00", query.stdout)
        self.assertIn("2026-09-25", query.stdout)

    def invoke_query_invoice(self, code: str) -> subprocess.CompletedProcess[str]:
        return self.cli.invoke("query-invoice", "--code", code)

    def test_invoice_invalid_amount_rejected(self) -> None:
        self.cli.confirm()
        for bad in ["0", "-1", "1.234", "abc"]:
            with self.subTest(bad=bad):
                result = self.cli.invoice(code=f"X-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("金额", result.stderr)

    def test_invoice_invalid_date_rejected(self) -> None:
        self.cli.confirm()
        result = self.cli.invoice(code="BAD-DATE", date="2026-13-40")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("日期", result.stderr)

    def test_failed_invoice_leaves_no_record(self) -> None:
        # 未确认就开票失败；确认后开票，之前失败尝试不应残留。
        self.assertNotEqual(self.cli.invoice(code="INV01").returncode, 0)
        self.cli.confirm()
        self.assertEqual(self.cli.invoice(code="INV01").returncode, 0)
        query = self.cli.query_contract()
        self.assertEqual(query.stdout.count("INV01"), 1)


class RegisterPaymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = BillingHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(self.cli.confirm().returncode, 0)
        self.assertEqual(self.cli.invoice(amount="1000").returncode, 0)

    def test_pay_by_invoice_success(self) -> None:
        result = self.cli.pay("PAY01", "400", invoice="INV01", date="2026-05-01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PAY01", result.stdout)
        self.assertIn("400.00", result.stdout)
        self.assertIn("匹配目标：INV01", result.stdout)

    def test_pay_by_contract_success(self) -> None:
        result = self.cli.pay("PAY01", "400", contract="C001")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("匹配目标：合同：C001", result.stdout)

    def test_both_targets_is_illegal_and_writes_nothing(self) -> None:
        result = self.cli.pay("PAY01", "10", invoice="INV01", contract="C001")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        query = self.cli.query_contract()
        self.assertIn("收款：无", query.stdout)

    def test_neither_target_is_illegal(self) -> None:
        result = self.cli.pay("PAY01", "10")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invoice", result.stderr.lower())

    def test_unknown_invoice_and_contract_rejected(self) -> None:
        self.assertNotEqual(self.cli.pay("PAY01", "10", invoice="NOPE").returncode, 0)
        self.assertNotEqual(self.cli.pay("PAY02", "10", contract="NOPE").returncode, 0)

    def test_overpay_rejected_entirely_and_record_kept(self) -> None:
        self.assertEqual(self.cli.pay("PAY01", "400", invoice="INV01").returncode, 0)
        # 400 + 700 = 1100 > 1000，整笔拒绝。
        over = self.cli.pay("PAY02", "700", invoice="INV01")
        self.assertNotEqual(over.returncode, 0)
        self.assertIn("超过发票金额", over.stderr)
        # 发票仍为部分收款、累计仍 400，且没有第二笔记录。
        query = self.cli.invoke("query-invoice", "--code", "INV01")
        self.assertIn("状态：部分收款", query.stdout)
        self.assertEqual(query.stdout.count("PAY"), 1)

    def test_exact_amount_closes_invoice(self) -> None:
        self.cli.pay("PAY01", "400", invoice="INV01")
        self.assertEqual(self.cli.pay("PAY02", "600", invoice="INV01").returncode, 0)
        query = self.cli.invoke("query-invoice", "--code", "INV01")
        self.assertIn("状态：已收齐", query.stdout)
        # 收齐后再收一分钱也要拒绝。
        self.assertNotEqual(self.cli.pay("PAY03", "0.01", invoice="INV01").returncode, 0)

    def test_duplicate_payment_is_idempotent(self) -> None:
        first = self.cli.pay("PAY01", "400", invoice="INV01", date="2026-05-01")
        self.assertEqual(first.returncode, 0, first.stderr)
        # 重复请求即便参数不同，也返回与首次相同的成功输出。
        again = self.cli.pay("PAY01", "9999", invoice="INV01")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout, first.stdout)
        query = self.cli.invoke("query-invoice", "--code", "INV01")
        self.assertEqual(query.stdout.count("PAY01"), 1)

    def test_contract_payment_has_no_invoice_cap(self) -> None:
        # 按合同匹配的收款不受任何发票金额限制。
        result = self.cli.pay("PAY01", "999999", contract="C001")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_payment_amount_rejected(self) -> None:
        for bad in ["0", "-5", "1.005"]:
            with self.subTest(bad=bad):
                result = self.cli.pay(f"X-{bad}", bad, invoice="INV01")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("金额", result.stderr)


class QueryContractBillingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = BillingHarness()
        self.addCleanup(self.cli.cleanup)
        self.cli.register_contract()
        self.cli.register_milestone()
        self.cli.confirm()

    def test_no_billing_records_shows_zero_totals(self) -> None:
        out = self.cli.query_contract().stdout
        self.assertIn("开票：无", out)
        self.assertIn("收款：无", out)
        self.assertIn("开票合计：0.00", out)
        self.assertIn("收款合计：0.00", out)
        self.assertTrue(out.rstrip().endswith("未匹配差额：0.00"))

    def test_invoice_statuses_in_registration_order(self) -> None:
        self.cli.invoice(code="INV01", amount="1000")
        self.cli.invoice(code="INV02", amount="500", date="2026-04-02")
        self.cli.pay("PAY01", "300", invoice="INV01")  # INV01 部分收款
        out = self.cli.query_contract().stdout
        self.assertLess(out.index("INV01"), out.index("INV02"))
        line1 = next(line for line in out.splitlines() if "INV01" in line)
        line2 = next(line for line in out.splitlines() if "INV02" in line)
        self.assertIn("状态：部分收款", line1)
        self.assertIn("状态：已开票", line2)
        self.assertIn("金额：1000.00", line1)
        self.assertIn("里程碑编号：MS01", line1)
        # 合计：开票 1500，收款 300，差额 1200。
        self.assertIn("开票合计：1500.00", out)
        self.assertIn("收款合计：300.00", out)
        self.assertIn("未匹配差额：1200.00", out)

    def test_contract_payment_can_make_diff_negative(self) -> None:
        self.cli.invoice(code="INV01", amount="100")
        self.cli.pay("PAY01", "250.50", contract="C001")
        out = self.cli.query_contract().stdout
        self.assertIn("开票合计：100.00", out)
        self.assertIn("收款合计：250.50", out)
        self.assertIn("未匹配差额：-150.50", out)

    def test_billing_scoped_per_contract(self) -> None:
        self.cli.register_contract(code="C002", amount="5000")
        self.cli.register_milestone(code="MS02", contract="C002", amount="1000")
        self.cli.confirm("MS02")
        self.cli.invoice(milestone="MS01", code="INV01", amount="1000")
        self.cli.invoice(milestone="MS02", code="INV02", amount="800")
        self.cli.pay("PAY01", "1000", invoice="INV01")
        self.cli.pay("PAY02", "800", contract="C002")
        c1 = self.cli.query_contract("C001").stdout
        c2 = self.cli.query_contract("C002").stdout
        self.assertIn("INV01", c1)
        self.assertNotIn("INV02", c1)
        self.assertNotIn("PAY02", c1)
        self.assertIn("INV02", c2)
        self.assertIn("PAY02", c2)
        self.assertNotIn("INV01", c2)
        self.assertIn("开票合计：1000.00", c1)
        self.assertIn("收款合计：1000.00", c1)
        self.assertIn("开票合计：800.00", c2)
        self.assertIn("收款合计：800.00", c2)

    def test_invoice_payment_through_milestone_counts_for_contract(self) -> None:
        # 按发票的收款经由 发票→里程碑→合同 归入所属合同收款段。
        self.cli.invoice(code="INV01", amount="1000")
        self.cli.pay("PAY01", "1000", invoice="INV01")
        out = self.cli.query_contract().stdout
        self.assertIn("PAY01", out)
        self.assertIn("匹配目标：INV01", out)
        self.assertIn("收款合计：1000.00", out)


class QueryInvoicePaymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = BillingHarness()
        self.addCleanup(self.cli.cleanup)
        self.cli.register_contract()
        self.cli.register_milestone()
        self.cli.confirm()
        self.cli.invoice(code="INV01", amount="1000", date="2026-04-01")

    def test_query_invoice_fields_and_empty_payments(self) -> None:
        out = self.cli.invoke("query-invoice", "--code", "INV01")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("里程碑编号：MS01", out.stdout)
        self.assertIn("金额：1000.00", out.stdout)
        self.assertIn("日期：2026-04-01", out.stdout)
        self.assertIn("状态：已开票", out.stdout)
        self.assertIn("收款明细：无", out.stdout)

    def test_query_invoice_lists_payments_in_order(self) -> None:
        self.cli.pay("PAY01", "300", invoice="INV01", date="2026-05-01")
        self.cli.pay("PAY02", "700", invoice="INV01", date="2026-05-02")
        out = self.cli.invoke("query-invoice", "--code", "INV01")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertLess(out.stdout.index("PAY01"), out.stdout.index("PAY02"))
        self.assertIn("金额：300.00  日期：2026-05-01", out.stdout)
        self.assertIn("状态：已收齐", out.stdout)

    def test_query_unknown_invoice_fails_on_stderr(self) -> None:
        out = self.cli.invoke("query-invoice", "--code", "NOPE")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("不存在", out.stderr)
        self.assertEqual(out.stdout, "")

    def test_query_payment_fields(self) -> None:
        self.cli.pay("PAY01", "300.25", invoice="INV01", date="2026-05-01")
        out = self.cli.invoke("query-payment", "--code", "PAY01")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("金额：300.25", out.stdout)
        self.assertIn("日期：2026-05-01", out.stdout)
        self.assertIn("匹配目标：INV01", out.stdout)

    def test_query_contract_payment_target(self) -> None:
        self.cli.pay("PAY09", "1", contract="C001")
        out = self.cli.invoke("query-payment", "--code", "PAY09")
        self.assertIn("匹配目标：合同：C001", out.stdout)

    def test_query_unknown_payment_fails_on_stderr(self) -> None:
        out = self.cli.invoke("query-payment", "--code", "NOPE")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("不存在", out.stderr)
        self.assertEqual(out.stdout, "")


if __name__ == "__main__":
    unittest.main()
