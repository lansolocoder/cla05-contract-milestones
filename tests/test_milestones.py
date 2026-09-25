"""付款里程碑登记、状态流转与金额联动的端到端 CLI 测试。

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


class MilestoneHarness:
    """在临时数据库上调用 python3 -m contract_ledger。"""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ledger-ms-test-")
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

    def register_change(self, contract="C001", code="CHG01",
                        description="追加需求", delta="2000"):
        return self.invoke(
            "register-change",
            "--contract", contract, "--code", code,
            "--description", description, "--delta", delta,
        )

    def query_contract(self, code="C001"):
        return self.invoke("query-contract", "--code", code)

    def query_milestone(self, code="MS01"):
        return self.invoke("query-milestone", "--code", code)


class MilestoneRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = MilestoneHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)

    def test_register_success_is_pending_and_listed_in_order(self) -> None:
        r1 = self.cli.register_milestone(code="MS1", title="首款", amount="3000")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertIn("待确认", r1.stdout)
        r2 = self.cli.register_milestone(code="MS2", title="尾款", amount="7000.50")
        self.assertEqual(r2.returncode, 0, r2.stderr)

        query = self.cli.query_contract()
        self.assertEqual(query.returncode, 0, query.stderr)
        self.assertIn("里程碑：", query.stdout)
        self.assertLess(query.stdout.index("MS1"), query.stdout.index("MS2"))
        self.assertIn("标题：首款", query.stdout)
        self.assertIn("当前金额：3000", query.stdout)
        self.assertIn("当前金额：7000.5", query.stdout)
        for line in query.stdout.splitlines():
            if line.strip().startswith("- MS"):
                self.assertIn("待确认", line)

    def test_milestone_on_nonexistent_contract_fails(self) -> None:
        result = self.cli.register_milestone(contract="GHOST", code="MS1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_duplicate_milestone_code_fails_globally(self) -> None:
        self.assertEqual(self.cli.register_milestone(code="DUP").returncode, 0)
        self.cli.register_contract(code="C002", customer="乙", amount="100")
        result = self.cli.register_milestone(contract="C002", code="DUP")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DUP", result.stderr)
        # 第二份合同没有留下任何里程碑。
        self.assertIn("里程碑：无", self.cli.query_contract("C002").stdout)

    def test_invalid_amounts_fail_before_write(self) -> None:
        for bad in ["0", "-1", "-0.01", "1.234", "abc", "1e3", "", " 100"]:
            with self.subTest(bad=bad):
                result = self.cli.register_milestone(code=f"E-{bad or 'empty'}", amount=bad)
                self.assertNotEqual(result.returncode, 0, f"应当拒绝 {bad!r}")
                self.assertIn("金额", result.stderr)
        # 对照：最小的合法正数可以登记。
        ok = self.cli.register_milestone(code="MS-MIN", amount="0.01")
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_blank_title_rejected(self) -> None:
        result = self.cli.register_milestone(code="MSB", title="   ")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("里程碑标题", result.stderr)

    def test_invalid_due_date_rejected(self) -> None:
        for bad in ["2026-13-01", "2026-02-30", "2026/03/01", "20260301", "2026-3-1"]:
            with self.subTest(bad=bad):
                result = self.cli.register_milestone(code=f"D-{bad}", due=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("到期日", result.stderr)

    def test_no_milestone_section_shows_none_and_zero_confirmed(self) -> None:
        query = self.cli.query_contract()
        self.assertIn("里程碑：无", query.stdout)
        self.assertIn("已确认金额：0\n", query.stdout)


class MilestoneAmountLinkageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = MilestoneHarness()
        self.addCleanup(self.cli.cleanup)

    def test_amount_scales_with_effective_change(self) -> None:
        # 合同 1000，里程碑 300；变更 +1000 生效 -> 合同 2000，里程碑 600。
        self.cli.register_contract(amount="1000")
        self.cli.register_milestone(amount="300")
        self.cli.register_change(delta="1000")
        self.assertEqual(
            self.cli.invoke("effect-change", "--code", "CHG01").returncode, 0
        )
        query = self.cli.query_milestone()
        self.assertIn("登记金额：300", query.stdout)
        self.assertIn("当前金额：600", query.stdout)

    def test_amount_restored_when_change_voided(self) -> None:
        self.cli.register_contract(amount="1000")
        self.cli.register_milestone(amount="300")
        self.cli.register_change(delta="1000")
        self.cli.invoke("effect-change", "--code", "CHG01")
        self.cli.invoke("void-change", "--code", "CHG01")
        query = self.cli.query_milestone()
        self.assertIn("当前金额：300", query.stdout)

    def test_amount_floors_to_cent(self) -> None:
        # 合同 300，里程碑 100（10000 分）；合同降到 200：
        # 10000*200//300 = 6666 分 = 66.66（向下取整）。
        self.cli.register_contract(amount="300")
        self.cli.register_milestone(amount="100")
        self.cli.register_change(delta="-100")
        self.cli.invoke("effect-change", "--code", "CHG01")
        query = self.cli.query_milestone()
        self.assertIn("当前金额：66.66", query.stdout)

    def test_amount_unchanged_when_contract_amount_unchanged(self) -> None:
        # 草稿变更单不影响合同金额，里程碑金额不变。
        self.cli.register_contract(amount="1000")
        self.cli.register_milestone(amount="300")
        self.cli.register_change(delta="5000")  # 仍是草稿
        query = self.cli.query_milestone()
        self.assertIn("当前金额：300", query.stdout)


class MilestoneLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = MilestoneHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract(amount="1000").returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)

    def test_confirm_pending(self) -> None:
        result = self.cli.invoke("confirm-milestone", "--code", "MS01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("已确认", result.stdout)
        query = self.cli.query_milestone()
        self.assertIn("状态：已确认", query.stdout)

    def test_cancel_pending(self) -> None:
        result = self.cli.invoke("cancel-milestone", "--code", "MS01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("已作废", result.stdout)

    def test_confirmed_can_be_canceled(self) -> None:
        self.cli.invoke("confirm-milestone", "--code", "MS01")
        result = self.cli.invoke("cancel-milestone", "--code", "MS01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("已作废", self.cli.query_milestone().stdout)

    def test_cannot_confirm_twice(self) -> None:
        self.cli.invoke("confirm-milestone", "--code", "MS01")
        again = self.cli.invoke("confirm-milestone", "--code", "MS01")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("已确认", again.stderr)

    def test_voided_is_terminal(self) -> None:
        self.cli.invoke("cancel-milestone", "--code", "MS01")
        confirm = self.cli.invoke("confirm-milestone", "--code", "MS01")
        self.assertNotEqual(confirm.returncode, 0)
        self.assertIn("作废", confirm.stderr)
        cancel = self.cli.invoke("cancel-milestone", "--code", "MS01")
        self.assertNotEqual(cancel.returncode, 0)
        self.assertIn("作废", cancel.stderr)
        # 状态保持已作废。
        self.assertIn("状态：已作废", self.cli.query_milestone().stdout)

    def test_unknown_code_fails_for_all_operations(self) -> None:
        for command in ("confirm-milestone", "cancel-milestone"):
            result = self.cli.invoke(command, "--code", "MISSING")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("不存在", result.stderr)

    def test_illegal_transition_changes_nothing(self) -> None:
        # 作废后再确认失败：状态仍是已作废，且无任何写入残留。
        self.cli.invoke("cancel-milestone", "--code", "MS01")
        before = self.cli.query_milestone().stdout
        bad = self.cli.invoke("confirm-milestone", "--code", "MS01")
        self.assertNotEqual(bad.returncode, 0)
        after = self.cli.query_milestone().stdout
        self.assertEqual(before, after)


class ConfirmedAmountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = MilestoneHarness()
        self.addCleanup(self.cli.cleanup)
        # 合同 2000；三个里程碑登记金额 500 / 500 / 1000。
        self.cli.register_contract(amount="2000")
        self.cli.register_milestone(code="M1", amount="500")
        self.cli.register_milestone(code="M2", amount="500")
        self.cli.register_milestone(code="M3", amount="1000")

    def test_only_confirmed_counted(self) -> None:
        self.cli.invoke("confirm-milestone", "--code", "M1")
        query = self.cli.query_contract()
        self.assertIn("已确认金额：500", query.stdout)

        self.cli.invoke("confirm-milestone", "--code", "M3")
        query = self.cli.query_contract()
        # M1 + M3 = 500 + 1000 = 1500；M2 待确认不计入。
        self.assertIn("已确认金额：1500", query.stdout)

    def test_voided_confirmed_no_longer_counted(self) -> None:
        self.cli.invoke("confirm-milestone", "--code", "M1")
        self.cli.invoke("confirm-milestone", "--code", "M2")
        self.cli.invoke("cancel-milestone", "--code", "M1")
        query = self.cli.query_contract()
        self.assertIn("已确认金额：500", query.stdout)

    def test_confirmed_amount_scales_with_contract(self) -> None:
        # M1 已确认 500；变更 +2000 生效使合同翻倍 -> 已确认金额 1000。
        self.cli.invoke("confirm-milestone", "--code", "M1")
        self.cli.register_change(delta="2000")
        self.cli.invoke("effect-change", "--code", "CHG01")
        query = self.cli.query_contract()
        self.assertIn("当前金额：4000", query.stdout)
        self.assertIn("已确认金额：1000", query.stdout)


class QueryMilestoneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = MilestoneHarness()
        self.addCleanup(self.cli.cleanup)

    def test_query_outputs_all_fields(self) -> None:
        self.cli.register_contract(
            code="ORD", customer="客户甲", signed="2026-02-01", amount="1000"
        )
        self.cli.register_milestone(
            contract="ORD", code="MSX", title="验收款", amount="250.5", due="2026-05-01"
        )
        result = self.cli.query_milestone("MSX")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("标题：验收款", result.stdout)
        self.assertIn("所属合同编号：ORD", result.stdout)
        self.assertIn("登记金额：250.5", result.stdout)
        self.assertIn("当前金额：250.5", result.stdout)
        self.assertIn("到期日：2026-05-01", result.stdout)
        self.assertIn("状态：待确认", result.stdout)

    def test_query_unknown_milestone_fails_on_stderr(self) -> None:
        result = self.cli.query_milestone("NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
