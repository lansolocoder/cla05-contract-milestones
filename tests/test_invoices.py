"""开票与收款的端到端 CLI 测试。

每个用例使用独立临时数据库（CONTRACT_LEDGER_DB），与其他测试隔离。
金额一律按整数分比较，日期按 YYYY-MM-DD 文本比较。
query-payment 输出为 JSON 数组，末尾元素为合同汇总。
"""

import json
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

    def register_contract(self, code="HT1", customer="甲方公司",
                          signed="2026-01-15", amount="10000"):
        return self.invoke(
            "register-contract", "--code", code, "--customer", customer,
            "--date", signed, "--amount", amount,
        )

    def register_milestone(self, contract="HT1", code="MS1", name="首付款",
                           amount="4000", due="2026-03-01"):
        return self.invoke(
            "register-milestone", "--contract", contract, "--code", code,
            "--name", name, "--amount", amount, "--due", due,
        )

    def issue_invoice(self, contract="HT1", invoice="INV1", milestone="MS1",
                      amount="4000", date="2026-02-01", flag="--code"):
        return self.invoke(
            "issue-invoice", "--contract", contract, flag, invoice,
            "--milestone", milestone, "--amount", amount, "--date", date,
        )

    def record_payment(self, invoice="INV1", code="PAY1",
                       amount="1000", date="2026-02-10"):
        return self.invoke(
            "record-payment", "--invoice", invoice, "--code", code,
            "--amount", amount, "--date", date,
        )

    def void_invoice(self, invoice="INV1"):
        return self.invoke("void-invoice", "--invoice", invoice)

    def query_payment(self, contract="HT1", as_of=None):
        args = ["query-payment", "--contract", contract]
        if as_of is not None:
            args += ["--as-of", as_of]
        return self.invoke(*args)

    def query_json(self, contract="HT1", as_of=None):
        result = self.query_payment(contract, as_of)
        if result.returncode != 0:
            raise AssertionError(f"query-payment 失败：{result.stderr}")
        return json.loads(result.stdout)


class InvoiceRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)

    def test_issue_success_outputs_invoice_milestone_amount(self) -> None:
        result = self.cli.issue_invoice(amount="4000.50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("发票号：INV1", result.stdout)
        self.assertIn("里程碑编号：MS1", result.stdout)
        self.assertIn("金额：4000.5", result.stdout)

    def test_accepts_invoice_flag_alias(self) -> None:
        result = self.cli.issue_invoice(invoice="INV-A", flag="--invoice")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INV-A", result.stdout)

    def test_accepts_positional_invoice_number(self) -> None:
        result = self.cli.invoke(
            "issue-invoice", "--contract", "HT1", "INV-POS",
            "--milestone", "MS1", "--amount", "100", "--date", "2026-02-01",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("发票号：INV-POS", result.stdout)

    def test_duplicate_invoice_rejected_and_keeps_data(self) -> None:
        self.assertEqual(self.cli.issue_invoice().returncode, 0)
        dup = self.cli.issue_invoice(invoice="INV1", amount="999")
        self.assertNotEqual(dup.returncode, 0)
        self.assertIn("INV1", dup.stderr)
        # 既有发票金额未被改动。
        data = self.cli.query_json()
        self.assertEqual(data[0]["amount"], 4000)

    def test_non_positive_or_three_decimal_amount_rejected(self) -> None:
        for bad in ["0", "-1", "0.00", "1.234", "abc", "1e3"]:
            with self.subTest(bad=bad):
                result = self.cli.issue_invoice(invoice=f"X-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("金额", result.stderr)

    def test_date_before_contract_signed_rejected(self) -> None:
        result = self.cli.issue_invoice(date="2026-01-14")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("签订日期", result.stderr)

    def test_date_equal_to_contract_signed_allowed(self) -> None:
        result = self.cli.issue_invoice(date="2026-01-15")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_contract_rejected(self) -> None:
        result = self.cli.issue_invoice(contract="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_unknown_milestone_rejected(self) -> None:
        result = self.cli.issue_invoice(milestone="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("里程碑编号不存在", result.stderr)

    def test_milestone_not_belonging_to_contract_rejected(self) -> None:
        self.cli.register_contract(code="HT2", customer="乙", amount="100")
        result = self.cli.issue_invoice(contract="HT2", invoice="INV9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不属于该合同", result.stderr)
        # 第二份合同下没有任何发票。
        query = self.cli.query_payment("HT2")
        self.assertEqual(query.returncode, 0, query.stderr)
        self.assertEqual(json.loads(query.stdout)[-1]["totalExcess"], 0)

    def test_failed_issue_leaves_no_invoice(self) -> None:
        before = self.cli.query_payment().stdout
        self.cli.issue_invoice(date="2020-01-01", invoice="LATER")
        after = self.cli.query_payment().stdout
        self.assertEqual(before, after)


class PaymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.cli.register_contract()
        self.cli.register_milestone()
        self.assertEqual(
            self.cli.issue_invoice(amount="1000").returncode, 0
        )

    def test_partial_payment_keeps_invoice_unpaid(self) -> None:
        result = self.cli.record_payment(amount="300")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.cli.query_json()
        row = data[0]
        self.assertEqual(row["status"], "unpaid")
        self.assertEqual(row["received"], 300)
        self.assertEqual(row["excess"], 0)
        self.assertEqual(data[-1]["totalExcess"], 0)

    def test_exact_payment_marks_paid(self) -> None:
        self.cli.record_payment(code="P1", amount="400.50", date="2026-02-10")
        self.cli.record_payment(code="P2", amount="599.50", date="2026-02-11")
        row = self.cli.query_json()[0]
        self.assertEqual(row["status"], "paid")
        self.assertEqual(row["received"], 1000)

    def test_overpayment_applies_invoice_amount_and_balances_excess(self) -> None:
        result = self.cli.record_payment(amount="1200")
        self.assertEqual(result.returncode, 0, result.stderr)
        # 入账 1000，超出 200 转记合同超额余额，不丢弃。
        self.assertIn("入账金额：1000", result.stdout)
        self.assertIn("超收金额：200", result.stdout)
        data = self.cli.query_json()
        row = data[0]
        self.assertEqual(row["status"], "paid")
        self.assertEqual(row["received"], 1000)
        self.assertEqual(row["excess"], 200)
        self.assertEqual(data[-1]["totalExcess"], 200)

    def test_excess_accumulates_after_invoice_paid(self) -> None:
        self.cli.record_payment(code="P1", amount="1000")
        # 已收齐，后续整笔都转超额。
        self.cli.record_payment(code="P2", amount="300")
        self.cli.record_payment(code="P3", amount="50")
        data = self.cli.query_json()
        self.assertEqual(data[0]["excess"], 350)
        self.assertEqual(data[-1]["totalExcess"], 350)

    def test_excess_spans_multiple_invoices_of_contract(self) -> None:
        self.cli.register_milestone(code="MS2", name="尾款",
                                   amount="500", due="2026-04-01")
        self.cli.issue_invoice(invoice="INV2", milestone="MS2",
                               amount="500", date="2026-03-01")
        # INV1 超 200；INV2 恰好收齐不产生超额。
        self.cli.record_payment(invoice="INV1", code="P1", amount="1200")
        self.cli.record_payment(invoice="INV2", code="P2", amount="500")
        data = self.cli.query_json()
        invoice_rows = data[:-1]
        self.assertEqual(len(invoice_rows), 2)
        self.assertEqual(invoice_rows[0]["invoice"], "INV1")  # 登记先后
        self.assertEqual(invoice_rows[1]["invoice"], "INV2")
        self.assertEqual(invoice_rows[1]["milestone"], "MS2")
        self.assertEqual(data[-1]["totalExcess"], 200)

    def test_duplicate_payment_code_rejected(self) -> None:
        self.cli.record_payment(code="DUP", amount="10")
        dup = self.cli.record_payment(code="DUP", amount="20")
        self.assertNotEqual(dup.returncode, 0)
        self.assertIn("DUP", dup.stderr)
        self.assertEqual(self.cli.query_json()[0]["received"], 10)

    def test_unknown_invoice_rejected(self) -> None:
        result = self.cli.record_payment(invoice="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_payment_date_before_invoice_date_rejected(self) -> None:
        result = self.cli.record_payment(date="2026-01-31")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("发票日期", result.stderr)
        self.assertEqual(self.cli.query_json()[0]["received"], 0)

    def test_non_positive_amount_rejected(self) -> None:
        for bad in ["0", "-100", "1.999"]:
            with self.subTest(bad=bad):
                result = self.cli.record_payment(code=f"Z-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)

    def test_failed_payment_changes_nothing(self) -> None:
        self.cli.record_payment(code="P1", amount="400")
        before = self.cli.query_payment().stdout
        # 重复单号 + 大额：即便会产生超额，也必须整体回滚。
        bad = self.cli.record_payment(code="P1", amount="9999")
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(self.cli.query_payment().stdout, before)

    def test_query_is_read_only(self) -> None:
        self.cli.record_payment(amount="10")
        first = self.cli.query_payment().stdout
        second = self.cli.query_payment().stdout
        self.assertEqual(first, second)

    def test_query_unknown_contract_fails(self) -> None:
        result = self.cli.query_payment("NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)
        self.assertEqual(result.stdout, "")


class VoidInvoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.cli.register_contract()
        self.cli.register_milestone()
        self.cli.issue_invoice(amount="1000")

    def test_void_unpaid_invoice_succeeds(self) -> None:
        result = self.cli.void_invoice()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("发票已作废：INV1", result.stdout)
        self.assertEqual(self.cli.query_json()[0]["status"], "void")

    def test_paid_invoice_cannot_be_voided(self) -> None:
        self.assertEqual(
            self.cli.record_payment(amount="1000").returncode, 0
        )
        result = self.cli.void_invoice()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("paid", result.stderr)
        # 状态保持 paid。
        self.assertEqual(self.cli.query_json()[0]["status"], "paid")

    def test_void_invoice_cannot_be_voided_again(self) -> None:
        self.assertEqual(self.cli.void_invoice().returncode, 0)
        again = self.cli.void_invoice()
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("作废", again.stderr)

    def test_void_unknown_invoice_rejected(self) -> None:
        result = self.cli.void_invoice("GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_void_invoice_rejects_later_payment(self) -> None:
        self.assertEqual(self.cli.void_invoice().returncode, 0)
        result = self.cli.record_payment(code="P1", amount="100")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("作废", result.stderr)
        row = self.cli.query_json()[0]
        self.assertEqual(row["status"], "void")
        self.assertEqual(row["received"], 0)

    def test_void_keeps_existing_payments_excess_and_contract_total(self) -> None:
        # 先超额收款 1200（入账 1000 -> paid，超收 200），再无法作废；
        # 改为部分收款 300（保持 unpaid，超收 0）后作废，金额数值全部保留。
        self.cli.record_payment(code="P1", amount="300")
        self.assertEqual(self.cli.void_invoice().returncode, 0)
        # 再登记第二张发票并超额收款，验证合同累计超额仍可累计且不受
        # 已作废发票影响。
        self.cli.register_milestone(code="MS2", name="尾款",
                                   amount="500", due="2026-04-01")
        self.cli.issue_invoice(invoice="INV2", milestone="MS2",
                               amount="500", date="2026-03-01")
        pay = self.cli.record_payment(invoice="INV2", code="P2",
                                      amount="700", date="2026-03-05")
        self.assertEqual(pay.returncode, 0, pay.stderr)
        data = self.cli.query_json()
        rows = {row["invoice"]: row for row in data[:-1]}
        self.assertEqual(rows["INV1"]["status"], "void")
        self.assertEqual(rows["INV1"]["received"], 300)
        self.assertEqual(rows["INV1"]["excess"], 0)
        self.assertEqual(rows["INV2"]["status"], "paid")
        self.assertEqual(rows["INV2"]["received"], 500)
        self.assertEqual(rows["INV2"]["excess"], 200)
        self.assertEqual(data[-1]["totalExcess"], 200)

    def test_failed_void_changes_nothing(self) -> None:
        self.cli.record_payment(code="P1", amount="300")
        # 把发票置为 paid 后作废必须失败，且全部数值不变。
        self.cli.record_payment(code="P2", amount="700")
        before = self.cli.query_json()
        bad = self.cli.void_invoice()
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(self.cli.query_json(), before)


class QueryPaymentJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.cli.register_contract()
        self.cli.register_milestone(due="2026-03-10")
        self.cli.issue_invoice(invoice="INV1", amount="1000.50",
                               date="2026-02-01")

    def test_json_shape_and_field_order(self) -> None:
        data = self.cli.query_json()
        self.assertEqual(len(data), 2)  # 一张发票 + 汇总
        self.assertEqual(
            list(data[0]),
            ["invoice", "milestone", "amount", "status", "received",
             "excess", "overdueDays", "overdueAmount"],
        )
        self.assertEqual(list(data[-1]),
                         ["totalExcess", "totalOverdueAmount"])
        row = data[0]
        self.assertEqual(row["invoice"], "INV1")
        self.assertEqual(row["milestone"], "MS1")
        self.assertEqual(row["amount"], 1000.5)
        self.assertEqual(row["status"], "unpaid")
        self.assertEqual(row["received"], 0)
        self.assertEqual(row["excess"], 0)
        self.assertEqual(row["overdueAmount"], 1000.5)
        self.assertEqual(data[-1]["totalExcess"], 0)
        self.assertEqual(data[-1]["totalOverdueAmount"], 1000.5)

    def test_as_of_overdue_days_and_zero_boundary(self) -> None:
        # 到期日 2026-03-10。
        self.assertEqual(self.cli.query_json(as_of="2026-03-09")[0]["overdueDays"], 0)
        self.assertEqual(self.cli.query_json(as_of="2026-03-10")[0]["overdueDays"], 0)
        self.assertEqual(self.cli.query_json(as_of="2026-03-11")[0]["overdueDays"], 1)
        self.assertEqual(self.cli.query_json(as_of="2026-04-09")[0]["overdueDays"], 30)

    def test_overdue_amount_only_for_unpaid(self) -> None:
        # 部分收款 300：逾期未收 = 1000.5 - 300 = 700.5。
        self.cli.record_payment(code="P1", amount="300")
        row = self.cli.query_json(as_of="2026-04-01")[0]
        self.assertEqual(row["status"], "unpaid")
        self.assertEqual(row["overdueAmount"], 700.5)
        # 收齐置 paid 后逾期未收归零，逾期天数仍按到期日计算。
        self.cli.record_payment(code="P2", amount="700.5")
        row = self.cli.query_json(as_of="2026-04-01")[0]
        self.assertEqual(row["status"], "paid")
        self.assertEqual(row["overdueAmount"], 0)
        self.assertEqual(row["overdueDays"], 22)
        # 作废后同样归零。
        self.cli.register_milestone(code="MS2", name="尾款",
                                   amount="100", due="2026-05-01")
        self.cli.issue_invoice(invoice="INV2", milestone="MS2",
                               amount="100", date="2026-03-01")
        self.cli.void_invoice("INV2")
        rows = {r["invoice"]: r for r in self.cli.query_json(as_of="2026-06-01")[:-1]}
        self.assertEqual(rows["INV2"]["status"], "void")
        self.assertEqual(rows["INV2"]["overdueAmount"], 0)
        self.assertEqual(rows["INV2"]["overdueDays"], 31)
        # 汇总只统计 unpaid 的逾期未收（两张均非 unpaid -> 0）。
        self.assertEqual(self.cli.query_json(as_of="2026-06-01")[-1]["totalOverdueAmount"], 0)

    def test_total_overdue_sums_all_unpaid_invoices(self) -> None:
        self.cli.register_milestone(code="MS2", name="尾款",
                                   amount="500", due="2026-04-01")
        self.cli.issue_invoice(invoice="INV2", milestone="MS2",
                               amount="500", date="2026-03-01")
        self.cli.record_payment(code="P1", amount="200")  # INV1 余 800.5 未收
        data = self.cli.query_json(as_of="2026-05-01")
        self.assertEqual(data[-1]["totalOverdueAmount"], 1300.5)

    def test_invalid_as_of_fails_with_empty_stdout(self) -> None:
        for bad in ["2026-13-01", "2026-02-30", "2026/03/01", "20260301",
                    "2026-3-1", "not-a-date", ""]:
            with self.subTest(bad=bad):
                result = self.cli.query_payment(as_of=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("观察日期", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_bad_as_of_does_not_modify_data(self) -> None:
        before = self.cli.query_json()
        self.cli.query_payment(as_of="2026-99-99")
        self.assertEqual(self.cli.query_json(), before)

    def test_unknown_contract_with_as_of_fails_cleanly(self) -> None:
        result = self.cli.query_payment("NOPE", as_of="2026-05-01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_empty_contract_summary_only(self) -> None:
        self.cli.register_contract(code="HT2", customer="乙", amount="100")
        data = self.cli.query_json("HT2")
        self.assertEqual(data, [{"totalExcess": 0, "totalOverdueAmount": 0}])


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)

    def test_invoice_and_payments_persist_across_processes(self) -> None:
        self.cli.register_contract()
        self.cli.register_milestone()
        self.cli.issue_invoice(amount="1000")
        self.cli.record_payment(code="P1", amount="1200")
        # 全新进程读取。
        query = self.cli.query_payment()
        self.assertEqual(query.returncode, 0, query.stderr)
        data = json.loads(query.stdout)
        self.assertEqual(data[0]["status"], "paid")
        self.assertEqual(data[0]["received"], 1000)
        self.assertEqual(data[-1]["totalExcess"], 200)


if __name__ == "__main__":
    unittest.main()
