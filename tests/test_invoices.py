"""开票登记、收款匹配与发票查询的端到端 CLI 测试。

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

    def register_milestone(self, contract="C001", code="MS1", name="首付款",
                           amount="4000", due="2026-03-01"):
        return self.invoke(
            "register-milestone",
            "--contract", contract, "--code", code,
            "--name", name, "--amount", amount, "--due", due,
        )

    def record_invoice(self, contract="C001", code="INV1", amount="6000",
                       date="2026-02-01", milestone=None):
        args = [
            "record-invoice",
            "--contract", contract, "--code", code,
            "--amount", amount, "--date", date,
        ]
        if milestone is not None:
            args += ["--milestone", milestone]
        return self.invoke(*args)

    def record_receipt(self, code, invoice="INV1", amount="1000",
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
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)

    def test_invoice_success_outputs_code_and_amount(self) -> None:
        result = self.cli.record_invoice(amount="1234.56")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("发票编号：INV1", result.stdout)
        self.assertIn("开票金额：1234.56", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_invoice_without_milestone_belongs_to_contract(self) -> None:
        self.assertEqual(self.cli.record_invoice().returncode, 0)
        result = self.cli.query_invoice()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("归属：合同", result.stdout)

    def test_invoice_with_milestone_belongs_to_milestone(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        result = self.cli.record_invoice(code="INVMS", milestone="MS1")
        self.assertEqual(result.returncode, 0, result.stderr)
        query = self.cli.query_invoice("INVMS")
        self.assertIn("归属：MS1", query.stdout)

    def test_duplicate_invoice_code_fails(self) -> None:
        self.assertEqual(self.cli.record_invoice().returncode, 0)
        second = self.cli.record_invoice(code="INV1", amount="100",
                                         date="2026-03-01")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("INV1", second.stderr)
        # 既有发票金额未被改动。
        self.assertIn("开票金额：6000", self.cli.query_invoice().stdout)

    def test_invoice_on_nonexistent_contract_fails(self) -> None:
        result = self.cli.record_invoice(contract="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_invoice_with_nonexistent_milestone_fails(self) -> None:
        result = self.cli.record_invoice(code="INVB", milestone="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("里程碑编号不存在", result.stderr)

    def test_milestone_of_other_contract_rejected(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(
            self.cli.register_contract(code="C002", customer="乙", amount="100").returncode,
            0,
        )
        result = self.cli.record_invoice(contract="C002", code="INVB",
                                         milestone="MS1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不属于", result.stderr)

    def test_invalid_amount_or_date_fails(self) -> None:
        for bad in ["0", "-1", "1.234", "abc", "", "10.999"]:
            with self.subTest(bad=bad):
                result = self.cli.record_invoice(code=f"A-{bad or 'empty'}",
                                                 amount=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("金额", result.stderr)
        for bad in ["2026-13-01", "2026/01/01", "20260101"]:
            with self.subTest(bad=bad):
                result = self.cli.record_invoice(code=f"D-{bad}", date=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("日期", result.stderr)

    def test_failed_invoice_leaves_no_record(self) -> None:
        bad = self.cli.record_invoice(contract="NOPE", code="G1")
        self.assertNotEqual(bad.returncode, 0)
        query = self.cli.query_invoice("G1")
        self.assertNotEqual(query.returncode, 0)
        self.assertEqual(query.stdout, "")


class RecordReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract(amount="1000").returncode, 0)
        self.assertEqual(
            self.cli.record_invoice(amount="1000", date="2026-02-01").returncode, 0
        )

    def test_receipt_success_output(self) -> None:
        result = self.cli.record_receipt("R1", amount="400")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("收款编号：R1", result.stdout)
        self.assertIn("发票编号：INV1", result.stdout)
        self.assertIn("本次收款：400", result.stdout)
        self.assertIn("累计收款：400", result.stdout)
        self.assertIn("未收余额：600", result.stdout)

    def test_multiple_receipts_accumulate(self) -> None:
        self.assertEqual(self.cli.record_receipt("R1", amount="400").returncode, 0)
        result = self.cli.record_receipt("R2", amount="350.5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("本次收款：350.5", result.stdout)
        self.assertIn("累计收款：750.5", result.stdout)
        self.assertIn("未收余额：249.5", result.stdout)

    def test_overpayment_allowed_with_negative_balance(self) -> None:
        # 超收一笔：1100 未超过 1200 上限，允许，未收余额为负。
        result = self.cli.record_receipt("R1", amount="1100")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("累计收款：1100", result.stdout)
        self.assertIn("未收余额：-100", result.stdout)

    def test_cap_boundary_exact_120_percent_allowed(self) -> None:
        self.assertEqual(self.cli.record_receipt("R1", amount="1000").returncode, 0)
        # 恰好到上限 1200，允许。
        result = self.cli.record_receipt("R2", amount="200")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("累计收款：1200", result.stdout)

    def test_receipt_above_cap_rejected_and_not_persisted(self) -> None:
        self.assertEqual(self.cli.record_receipt("R1", amount="1000").returncode, 0)
        rejected = self.cli.record_receipt("R2", amount="200.01")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("20%", rejected.stderr)
        # 本次收款不落库：累计仍是 1000，且查不到 R2。
        query = self.cli.query_invoice()
        self.assertIn("累计收款：1000", query.stdout)
        self.assertNotIn("R2", query.stdout)

    def test_cap_uses_floor_cents_for_fractional_invoice(self) -> None:
        # 发票 0.03 元（3 分），上限 = 3*120//100 = 3 分（0.036 向下取整）。
        self.assertEqual(
            self.cli.record_invoice(code="CENT", amount="0.03").returncode, 0
        )
        self.assertEqual(
            self.cli.record_receipt("C1", invoice="CENT", amount="0.03").returncode, 0
        )
        # 再多收 1 分即超出 3 分上限。
        over = self.cli.record_receipt("C2", invoice="CENT", amount="0.01")
        self.assertNotEqual(over.returncode, 0)
        self.assertIn("20%", over.stderr)

    def test_receipt_before_invoice_date_rejected(self) -> None:
        result = self.cli.record_receipt("R1", amount="10", date="2026-01-31")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不得早于开票日期", result.stderr)

    def test_receipt_on_invoice_date_allowed(self) -> None:
        result = self.cli.record_receipt("R1", amount="10", date="2026-02-01")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_duplicate_receipt_code_fails(self) -> None:
        self.assertEqual(self.cli.record_receipt("DUP", amount="10").returncode, 0)
        second = self.cli.record_receipt("DUP", amount="20")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("DUP", second.stderr)
        # 第二笔未写入。
        self.assertIn("累计收款：10", self.cli.query_invoice().stdout)

    def test_receipt_on_nonexistent_invoice_fails(self) -> None:
        result = self.cli.record_receipt("R1", invoice="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("发票编号不存在", result.stderr)

    def test_invalid_amount_or_date_fails(self) -> None:
        for bad in ["0", "-1", "1.234", "abc"]:
            with self.subTest(bad=bad):
                result = self.cli.record_receipt(f"X-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("金额", result.stderr)
        bad = self.cli.record_receipt("XD", date="2026-2-1")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("日期", bad.stderr)

    def test_rejected_receipt_does_not_touch_existing_data(self) -> None:
        self.assertEqual(self.cli.record_receipt("R1", amount="1000").returncode, 0)
        # 触发上限拒绝。
        self.assertNotEqual(
            self.cli.record_receipt("R2", amount="201").returncode, 0
        )
        # 既有收款、发票金额与既有合同数据均保持原样。
        query = self.cli.query_invoice()
        self.assertIn("开票金额：1000", query.stdout)
        self.assertIn("累计收款：1000", query.stdout)
        self.assertIn("未收余额：0", query.stdout)
        contract = self.cli.invoke("query-contract", "--code", "C001")
        self.assertIn("当前金额：1000", contract.stdout)


class QueryInvoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(
            self.cli.record_invoice(
                code="INVQ", amount="6000", date="2026-02-01", milestone="MS1"
            ).returncode,
            0,
        )

    def test_unknown_invoice_fails(self) -> None:
        result = self.cli.query_invoice("NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_query_lists_all_fields(self) -> None:
        result = self.cli.query_invoice("INVQ")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("发票编号：INVQ", result.stdout)
        self.assertIn("合同编号：C001", result.stdout)
        self.assertIn("开票金额：6000", result.stdout)
        self.assertIn("开票日期：2026-02-01", result.stdout)
        self.assertIn("归属：MS1", result.stdout)
        self.assertIn("累计收款：0", result.stdout)
        self.assertIn("未收余额：6000", result.stdout)
        self.assertIn("收款：无", result.stdout)

    def test_receipts_listed_in_registration_order(self) -> None:
        self.assertEqual(
            self.cli.record_receipt("RA", invoice="INVQ", amount="100",
                                    date="2026-02-10").returncode, 0
        )
        self.assertEqual(
            self.cli.record_receipt("RB", invoice="INVQ", amount="200",
                                    date="2026-02-11").returncode, 0
        )
        self.assertEqual(
            self.cli.record_receipt("RC", invoice="INVQ", amount="300",
                                    date="2026-02-12").returncode, 0
        )
        result = self.cli.query_invoice("INVQ")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("累计收款：600", result.stdout)
        self.assertIn("未收余额：5400", result.stdout)
        # 按登记先后逐行列出：收款编号、收款日期、收款金额。
        self.assertLess(result.stdout.index("RA"), result.stdout.index("RB"))
        self.assertLess(result.stdout.index("RB"), result.stdout.index("RC"))
        self.assertIn("RA", result.stdout)
        self.assertIn("收款日期：2026-02-10", result.stdout)
        self.assertIn("收款金额：100", result.stdout)

    def test_query_does_not_modify_data(self) -> None:
        self.assertEqual(
            self.cli.record_receipt("RA", invoice="INVQ", amount="100").returncode, 0
        )
        before = self.cli.query_invoice("INVQ").stdout
        after = self.cli.query_invoice("INVQ").stdout
        self.assertEqual(before, after)


class PersistenceTests(unittest.TestCase):
    """跨进程：登记后在新进程中仍能读到发票与收款。"""

    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)

    def test_invoice_and_receipts_persist(self) -> None:
        self.assertEqual(self.cli.register_contract(code="LIFE").returncode, 0)
        self.assertEqual(
            self.cli.record_invoice(contract="LIFE", code="ILIFE",
                                    amount="500.5").returncode, 0
        )
        self.assertEqual(
            self.cli.record_receipt("RLIFE", invoice="ILIFE",
                                    amount="500.5").returncode, 0
        )
        result = self.cli.query_invoice("ILIFE")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("开票金额：500.5", result.stdout)
        self.assertIn("累计收款：500.5", result.stdout)
        self.assertIn("未收余额：0", result.stdout)
        self.assertIn("RLIFE", result.stdout)


if __name__ == "__main__":
    unittest.main()
