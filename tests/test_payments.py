"""收款登记（record-payment）与查询收款段的端到端 CLI 测试。

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
            "--contract", contract, "--code", code, "--name", name,
            "--amount", amount, "--due", due,
        )

    def record_payment(self, contract="C001", code="PAY01",
                       amount="1000", date="2026-02-01", milestone=None):
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

    def test_record_success_outputs_code_and_total(self) -> None:
        result = self.cli.record_payment(amount="1234.50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("收款单编号：PAY01", result.stdout)
        self.assertIn("收款合计：1234.5", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_contract_level_total_accumulates(self) -> None:
        self.assertEqual(self.cli.record_payment(code="P1", amount="100").returncode, 0)
        result = self.cli.record_payment(code="P2", amount="250.25")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("收款合计：350.25", result.stdout)

    def test_milestone_payment_total_uses_milestone_scope(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        # 合同级收款不计入里程碑口径。
        self.assertEqual(self.cli.record_payment(code="P1", amount="8000").returncode, 0)
        result = self.cli.record_payment(code="P2", amount="1500", milestone="MS01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("收款合计：1500", result.stdout)
        result = self.cli.record_payment(code="P3", amount="500", milestone="MS01")
        self.assertIn("收款合计：2000", result.stdout)

    def test_duplicate_payment_code_fails_and_keeps_data(self) -> None:
        self.assertEqual(self.cli.record_payment(amount="100").returncode, 0)
        dup = self.cli.record_payment(code="PAY01", amount="200", date="2026-02-02")
        self.assertNotEqual(dup.returncode, 0)
        self.assertIn("PAY01", dup.stderr)
        query = self.cli.query()
        self.assertIn("收款合计：100", query.stdout)
        self.assertNotIn("金额：200", query.stdout)

    def test_payment_on_nonexistent_contract_fails(self) -> None:
        result = self.cli.record_payment(contract="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_invalid_amounts_fail(self) -> None:
        for bad in ["0", "-1", "1.234", "abc", "1e3", "", " 100"]:
            with self.subTest(bad=bad):
                result = self.cli.record_payment(
                    code=f"P-{bad or 'empty'}", amount=bad
                )
                self.assertNotEqual(result.returncode, 0, f"应当拒绝 {bad!r}")
                self.assertIn("金额", result.stderr)
        self.assertIn("收款：无", self.cli.query().stdout)

    def test_invalid_dates_fail(self) -> None:
        for bad in ["2026-13-01", "2026-02-30", "2026/01/01", "2026-1-1"]:
            with self.subTest(bad=bad):
                result = self.cli.record_payment(code=f"D-{bad}", date=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("日期", result.stderr)

    def test_date_before_signed_date_fails(self) -> None:
        result = self.cli.record_payment(date="2026-01-14")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("签订日期", result.stderr)
        # 签订日当天可以收款。
        ok = self.cli.record_payment(code="P-ON", date="2026-01-15")
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_unknown_milestone_fails(self) -> None:
        result = self.cli.record_payment(milestone="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_milestone_of_other_contract_fails(self) -> None:
        self.cli.register_contract(code="C002", customer="乙", amount="100")
        self.cli.register_milestone(contract="C002", code="MSB", amount="50")
        result = self.cli.record_payment(milestone="MSB")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不属于", result.stderr)
        self.assertIn("收款：无", self.cli.query().stdout)

    def test_contract_level_overpayment_rejected(self) -> None:
        self.assertEqual(self.cli.record_payment(code="P1", amount="9999.99").returncode, 0)
        result = self.cli.record_payment(code="P2", amount="0.02")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("超", result.stderr)
        # 恰好等于合同当前金额可以登记。
        ok = self.cli.record_payment(code="P3", amount="0.01")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("收款合计：10000", ok.stdout)

    def test_milestone_payments_count_toward_contract_cap(self) -> None:
        # 归属里程碑的收款同样计入合同级上限。
        self.cli.register_contract(code="C002", customer="乙", amount="100")
        self.cli.register_milestone(contract="C002", code="MSB", amount="100")
        self.assertEqual(
            self.cli.record_payment(
                contract="C002", code="P1", amount="100", milestone="MSB"
            ).returncode,
            0,
        )
        result = self.cli.record_payment(contract="C002", code="P2", amount="0.01")
        self.assertNotEqual(result.returncode, 0)

    def test_milestone_level_overpayment_rejected(self) -> None:
        self.assertEqual(self.cli.register_milestone(amount="4000").returncode, 0)
        self.assertEqual(
            self.cli.record_payment(code="P1", amount="4000", milestone="MS01").returncode,
            0,
        )
        result = self.cli.record_payment(code="P2", amount="0.01", milestone="MS01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("超", result.stderr)
        # 合同级口径仍有余额，合同级收款不受影响。
        ok = self.cli.record_payment(code="P3", amount="100")
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_failed_payment_leaves_no_half_write(self) -> None:
        bad = self.cli.record_payment(code="P1", amount="99999")
        self.assertNotEqual(bad.returncode, 0)
        query = self.cli.query()
        self.assertIn("收款：无", query.stdout)
        self.assertIn("当前金额：10000", query.stdout)


class QueryPaymentSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)

    def test_no_payments_shows_placeholder_without_total(self) -> None:
        result = self.cli.query()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("收款：无", result.stdout)
        self.assertNotIn("收款合计", result.stdout)

    def test_payments_listed_in_registration_order(self) -> None:
        self.cli.register_milestone()
        self.cli.record_payment(code="P1", amount="100.10", date="2026-02-01")
        self.cli.record_payment(code="P2", amount="200", date="2026-02-02",
                                milestone="MS01")
        result = self.cli.query()
        self.assertEqual(result.returncode, 0, result.stderr)
        # 合同级合计含归属里程碑的收款单。
        self.assertIn("收款合计：300.10", result.stdout)
        self.assertIn("- P1  金额：100.10  日期：2026-02-01  归属：合同", result.stdout)
        self.assertIn("- P2  金额：200  日期：2026-02-02  归属：MS01", result.stdout)
        self.assertLess(result.stdout.index("- P1"), result.stdout.index("- P2"))

    def test_query_does_not_modify_data(self) -> None:
        self.cli.record_payment(amount="100")
        before = self.cli.query().stdout
        after = self.cli.query().stdout
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
