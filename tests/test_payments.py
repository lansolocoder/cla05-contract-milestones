"""收款登记（record-payment）与查询收款段的端到端 CLI 测试。

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
        self.tmp = tempfile.mkdtemp(prefix="ledger-payment-test-")
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

    def record_payment(self, contract="C001", code="PAY1", amount="1000",
                       date="2026-02-01", milestone=None):
        args = [
            "record-payment",
            "--contract", contract, "--code", code,
            "--amount", amount, "--date", date,
        ]
        if milestone is not None:
            args += ["--milestone", milestone]
        return self.invoke(*args)

    def query(self, code="C001"):
        return self.invoke("query-contract", "--code", code)


class RecordPaymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)

    def test_contract_level_success(self) -> None:
        result = self.cli.record_payment(amount="3000.50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("收款单编号：PAY1", result.stdout)
        self.assertIn("收款合计：3000.50", result.stdout)

    def test_contract_level_total_includes_milestone_payments(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(
            self.cli.record_payment(code="P1", amount="1000",
                                    milestone="MS1").returncode,
            0,
        )
        result = self.cli.record_payment(code="P2", amount="2000")
        self.assertEqual(result.returncode, 0, result.stderr)
        # 合同级口径合计 = 合同级收款 + 归属里程碑收款。
        self.assertIn("收款合计：3000", result.stdout)

    def test_milestone_level_total_is_milestone_scoped(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(
            self.cli.record_payment(code="P1", amount="1000",
                                    milestone="MS1").returncode,
            0,
        )
        self.assertEqual(
            self.cli.record_payment(code="P2", amount="500").returncode, 0
        )
        result = self.cli.record_payment(code="P3", amount="2000",
                                         milestone="MS1")
        self.assertEqual(result.returncode, 0, result.stderr)
        # 里程碑口径合计只含该里程碑的收款单。
        self.assertIn("收款合计：3000", result.stdout)

    def test_amount_formatting_has_no_trailing_zeros(self) -> None:
        result = self.cli.record_payment(amount="1000.10")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("收款合计：1000.1", result.stdout)

    def test_duplicate_code_rejected_and_data_unchanged(self) -> None:
        self.assertEqual(self.cli.record_payment(amount="1000").returncode, 0)
        bad = self.cli.record_payment(amount="9999")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("PAY1", bad.stderr)
        query = self.cli.query().stdout
        self.assertIn("收款合计：1000", query)
        self.assertNotIn("9999", query)

    def test_duplicate_code_rejected_globally(self) -> None:
        self.assertEqual(self.cli.record_payment().returncode, 0)
        self.assertEqual(
            self.cli.register_contract(code="C002", customer="乙",
                                       amount="100").returncode,
            0,
        )
        result = self.cli.record_payment(contract="C002", amount="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("收款：无", self.cli.query("C002").stdout)

    def test_non_positive_amount_rejected(self) -> None:
        for bad in ["0", "-1", "0.00"]:
            with self.subTest(bad=bad):
                result = self.cli.record_payment(code=f"A-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("金额", result.stderr)
        self.assertIn("收款：无", self.cli.query().stdout)

    def test_amount_too_many_decimals_rejected(self) -> None:
        result = self.cli.record_payment(amount="1.005")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("金额", result.stderr)

    def test_invalid_date_rejected(self) -> None:
        for bad in ["2026-13-01", "2026/02/01", "20260201", "2026-2-1"]:
            with self.subTest(bad=bad):
                result = self.cli.record_payment(code=f"D-{bad}", date=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("收款日期", result.stderr)

    def test_date_before_signed_date_rejected(self) -> None:
        result = self.cli.record_payment(date="2026-01-14")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("签订日期", result.stderr)
        self.assertIn("收款：无", self.cli.query().stdout)

    def test_date_equal_to_signed_date_allowed(self) -> None:
        result = self.cli.record_payment(date="2026-01-15")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_contract_rejected(self) -> None:
        result = self.cli.record_payment(contract="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_unknown_milestone_rejected(self) -> None:
        result = self.cli.record_payment(milestone="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_milestone_of_other_contract_rejected(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(
            self.cli.register_contract(code="C002", customer="乙",
                                       amount="100").returncode,
            0,
        )
        result = self.cli.record_payment(contract="C002", amount="1",
                                         milestone="MS1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不属于", result.stderr)
        self.assertIn("收款：无", self.cli.query("C002").stdout)

    def test_contract_level_overpayment_rejected(self) -> None:
        self.assertEqual(self.cli.record_payment(code="P1", amount="9000").returncode, 0)
        result = self.cli.record_payment(code="P2", amount="1000.01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("超", result.stderr)
        query = self.cli.query().stdout
        self.assertIn("收款合计：9000", query)
        self.assertNotIn("P2", query)

    def test_contract_level_limit_counts_milestone_payments(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(
            self.cli.record_payment(code="P1", amount="4000",
                                    milestone="MS1").returncode,
            0,
        )
        self.assertEqual(self.cli.record_payment(code="P2", amount="6000").returncode, 0)
        # 合同级合计已达 10000，再收 0.01 即超收。
        result = self.cli.record_payment(code="P3", amount="0.01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("收款合计：10000", self.cli.query().stdout)

    def test_milestone_level_overpayment_rejected(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        result = self.cli.record_payment(amount="4000.01", milestone="MS1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("超", result.stderr)
        self.assertIn("收款：无", self.cli.query().stdout)

    def test_milestone_level_exact_limit_allowed(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        result = self.cli.record_payment(amount="4000", milestone="MS1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("收款合计：4000", result.stdout)

    def test_milestone_limit_follows_effective_changes(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        # 生效 -1000 后里程碑当前金额为 3000，收款上限随之收紧。
        self.assertEqual(
            self.cli.register_change(code="CH1", delta="-1000",
                                     milestone="MS1").returncode,
            0,
        )
        self.assertEqual(self.cli.effect("CH1").returncode, 0)
        result = self.cli.record_payment(amount="3000.01", milestone="MS1")
        self.assertNotEqual(result.returncode, 0)
        ok = self.cli.record_payment(amount="3000", milestone="MS1")
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_contract_limit_follows_effective_changes(self) -> None:
        self.assertEqual(
            self.cli.register_change(code="CH1", delta="-100").returncode, 0
        )
        self.assertEqual(self.cli.effect("CH1").returncode, 0)
        result = self.cli.record_payment(amount="9900.01")
        self.assertNotEqual(result.returncode, 0)
        ok = self.cli.record_payment(amount="9900")
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_draft_and_void_changes_do_not_tighten_limit(self) -> None:
        self.assertEqual(
            self.cli.register_change(code="D1", delta="-5000").returncode, 0
        )
        result = self.cli.record_payment(amount="10000")
        self.assertEqual(result.returncode, 0, result.stderr)


class PaymentQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)

    def test_no_payments_shows_none(self) -> None:
        result = self.cli.query()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("收款：无", result.stdout)
        self.assertNotIn("收款合计", result.stdout)

    def test_payments_listed_in_registration_order(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(
            self.cli.record_payment(code="P1", amount="1000.50",
                                    date="2026-02-01").returncode,
            0,
        )
        self.assertEqual(
            self.cli.record_payment(code="P2", amount="2000",
                                    date="2026-03-01",
                                    milestone="MS1").returncode,
            0,
        )
        result = self.cli.query()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("收款合计：3000.50", result.stdout)
        self.assertIn(
            "- P1  金额：1000.50  日期：2026-02-01  归属：合同",
            result.stdout,
        )
        self.assertIn(
            "- P2  金额：2000  日期：2026-03-01  归属：MS1",
            result.stdout,
        )
        self.assertLess(
            result.stdout.index("- P1"), result.stdout.index("- P2")
        )

    def test_query_unknown_contract_still_fails(self) -> None:
        result = self.cli.query("GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)


if __name__ == "__main__":
    unittest.main()
