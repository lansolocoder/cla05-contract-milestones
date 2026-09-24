"""合同登记与变更管理的端到端 CLI 测试。

每个测试用例使用独立临时数据库（通过 CONTRACT_LEDGER_DB 环境变量
指定），测试结束后清理，不污染项目内默认数据库。
"""

from datetime import date, timedelta
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

    def register_change(self, contract="C001", code="CHG01",
                        description="追加需求", delta="2000",
                        milestone=None):
        args = [
            "register-change",
            "--contract", contract, "--code", code,
            "--description", description, "--delta", delta,
        ]
        if milestone is not None:
            args += ["--milestone", milestone]
        return self.invoke(*args)

    def register_milestone(self, contract="C001", code="MS01",
                           name="首付款", amount="3000", due="2026-06-30"):
        return self.invoke(
            "register-milestone",
            "--contract", contract, "--code", code, "--name", name,
            "--amount", amount, "--due", due,
        )

    def set_milestone_due(self, code="MS01", due="2026-07-31"):
        return self.invoke("set-milestone-due", "--code", code, "--due", due)

    def query(self, code="C001"):
        return self.invoke("query-contract", "--code", code)


class ContractRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)

    def test_register_success_outputs_code_and_current_amount(self) -> None:
        result = self.cli.register_contract(amount="12345.67")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("C001", result.stdout)
        self.assertIn("12345.67", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_duplicate_contract_fails_and_keeps_data(self) -> None:
        self.assertEqual(self.cli.register_contract(amount="10000").returncode, 0)
        second = self.cli.register_contract(
            code="C001", customer="另一家公司", signed="2026-02-01", amount="999"
        )
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("C001", second.stderr)

        result = self.cli.query()
        self.assertEqual(result.returncode, 0, result.stderr)
        # 既有数据未被改动：仍是原客户、原金额。
        self.assertIn("甲方公司", result.stdout)
        self.assertIn("初始金额：10000", result.stdout)
        self.assertIn("当前金额：10000", result.stdout)

    def test_invalid_amounts_fail_before_write(self) -> None:
        for bad in ["0", "-1", "1.234", "abc", "1e3", "", " 100", "10.999"]:
            with self.subTest(bad=bad):
                result = self.cli.register_contract(
                    code=f"C-{bad or 'empty'}", amount=bad
                )
                self.assertNotEqual(result.returncode, 0, f"应当拒绝 {bad!r}")
                self.assertIn("金额", result.stderr)
        # 对照：最小的合法正数可以登记。
        ok = self.cli.register_contract(code="C-MIN", amount="0.01")
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_invalid_date_fails(self) -> None:
        for bad in ["2026-13-01", "2026-02-30", "2026/01/01", "20260101", "2026-1-1"]:
            with self.subTest(bad=bad):
                result = self.cli.register_contract(code=f"D-{bad}", signed=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("日期", result.stderr)

    def test_missing_required_field_is_argparse_error(self) -> None:
        result = self.cli.invoke(
            "register-contract", "--code", "C9", "--customer", "X",
            "--date", "2026-01-01",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--amount", result.stderr)

    def test_blank_code_or_customer_fails(self) -> None:
        for field, args in [
            ("合同编号", ["--code", "  ", "--customer", "甲方"]),
            ("客户名称", ["--code", "C8", "--customer", " "]),
        ]:
            with self.subTest(field=field):
                result = self.cli.invoke(
                    "register-contract", *args,
                    "--date", "2026-01-01", "--amount", "100",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(field, result.stderr)

    def test_persistence_across_processes(self) -> None:
        first = self.cli.register_contract(code="PERSIST", amount="500.5")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.cli.query("PERSIST")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("500.5", second.stdout)


class ChangeRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)

    def test_draft_change_does_not_affect_amount(self) -> None:
        result = self.cli.register_change(delta="3000")
        self.assertEqual(result.returncode, 0, result.stderr)
        query = self.cli.query()
        self.assertIn("当前金额：10000", query.stdout)
        self.assertIn("草稿", query.stdout)

    def test_change_on_nonexistent_contract_fails(self) -> None:
        result = self.cli.register_change(contract="NOPE", code="X1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_duplicate_change_code_fails_globally(self) -> None:
        self.assertEqual(
            self.cli.register_change(code="DUP", delta="1").returncode, 0
        )
        self.cli.register_contract(code="C002", customer="乙", amount="100")
        result = self.cli.register_change(
            contract="C002", code="DUP", delta="2"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DUP", result.stderr)
        # 第二份合同没有留下任何变更单。
        query = self.cli.query("C002")
        self.assertIn("变更单：无", query.stdout)

    def test_zero_delta_rejected(self) -> None:
        for bad in ["0", "0.00", "-0"]:
            with self.subTest(bad=bad):
                result = self.cli.register_change(
                    code=f"Z-{bad}", delta=bad
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("零", result.stderr)

    def test_delta_too_many_decimals_rejected(self) -> None:
        result = self.cli.register_change(code="Z1", delta="1.005")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("金额", result.stderr)

    def test_blank_description_rejected(self) -> None:
        result = self.cli.register_change(code="B1", description="   ")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("变更说明", result.stderr)

    def test_failed_change_registration_leaves_no_half_write(self) -> None:
        # 引用不存在合同：失败后查询原合同仍无变更单。
        bad = self.cli.register_change(contract="GHOST", code="G1", delta="10")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("变更单：无", self.cli.query().stdout)


class ChangeLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract(amount="10000").returncode, 0)

    def test_effect_adds_positive_delta(self) -> None:
        self.assertEqual(self.cli.register_change(delta="3000").returncode, 0)
        result = self.cli.invoke("effect-change", "--code", "CHG01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("当前金额：13000", result.stdout)
        query = self.cli.query()
        self.assertIn("当前金额：13000", query.stdout)
        self.assertIn("已生效", query.stdout)

    def test_effect_adds_negative_delta(self) -> None:
        self.assertEqual(self.cli.register_change(delta="-2500").returncode, 0)
        result = self.cli.invoke("effect-change", "--code", "CHG01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("当前金额：7500", result.stdout)

    def test_effect_rejected_when_result_zero(self) -> None:
        self.cli.register_change(delta="-10000")
        result = self.cli.invoke("effect-change", "--code", "CHG01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("小于等于零", result.stderr)
        query = self.cli.query()
        self.assertIn("当前金额：10000", query.stdout)
        self.assertIn("草稿", query.stdout)

    def test_effect_rejected_when_result_negative(self) -> None:
        self.cli.register_change(delta="-20000")
        result = self.cli.invoke("effect-change", "--code", "CHG01")
        self.assertNotEqual(result.returncode, 0)
        query = self.cli.query()
        self.assertIn("当前金额：10000", query.stdout)
        self.assertIn("草稿", query.stdout)

    def test_cannot_effect_twice(self) -> None:
        self.cli.register_change(delta="10")
        self.assertEqual(
            self.cli.invoke("effect-change", "--code", "CHG01").returncode, 0
        )
        again = self.cli.invoke("effect-change", "--code", "CHG01")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("已生效", again.stderr)
        # 金额没有被重复计入。
        self.assertIn("当前金额：10010", self.cli.query().stdout)

    def test_effect_unknown_change_fails(self) -> None:
        result = self.cli.invoke("effect-change", "--code", "MISSING")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_void_effective_removes_delta(self) -> None:
        self.cli.register_change(delta="3000")
        self.cli.invoke("effect-change", "--code", "CHG01")
        result = self.cli.invoke("void-change", "--code", "CHG01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("当前金额：10000", result.stdout)
        query = self.cli.query()
        self.assertIn("已作废", query.stdout)
        self.assertIn("当前金额：10000", query.stdout)

    def test_void_effective_rejected_when_result_not_positive(self) -> None:
        # 初始 1000：+5000 生效 -> 6000；-5900 生效 -> 100；
        # 作废 +5000 将移除正向变动，余额变 -4900，必须拒绝。
        self.cli.register_contract(code="C002", customer="乙", amount="1000")
        self.cli.register_change(contract="C002", code="P1", delta="5000")
        self.cli.invoke("effect-change", "--code", "P1")
        self.cli.register_change(contract="C002", code="N1", delta="-5900")
        self.cli.invoke("effect-change", "--code", "N1")
        self.assertIn("当前金额：100", self.cli.query("C002").stdout)

        result = self.cli.invoke("void-change", "--code", "P1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("小于等于零", result.stderr)
        # 金额与状态均不变。
        query = self.cli.query("C002")
        self.assertIn("当前金额：100", query.stdout)
        self.assertIn("已生效", query.stdout)

    def test_void_draft_keeps_amount(self) -> None:
        self.cli.register_change(delta="9999")
        result = self.cli.invoke("void-change", "--code", "CHG01")
        self.assertEqual(result.returncode, 0, result.stderr)
        query = self.cli.query()
        self.assertIn("当前金额：10000", query.stdout)
        self.assertIn("已作废", query.stdout)
        # 草稿作废即使金额巨大也不校验余额（金额本就未计入）。

    def test_voided_change_cannot_be_effected(self) -> None:
        self.cli.register_change(delta="10")
        self.cli.invoke("void-change", "--code", "CHG01")
        result = self.cli.invoke("effect-change", "--code", "CHG01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("作废", result.stderr)
        self.assertIn("当前金额：10000", self.cli.query().stdout)

    def test_cannot_void_twice(self) -> None:
        self.cli.register_change(delta="10")
        self.cli.invoke("void-change", "--code", "CHG01")
        again = self.cli.invoke("void-change", "--code", "CHG01")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("作废", again.stderr)

    def test_void_unknown_change_fails(self) -> None:
        result = self.cli.invoke("void-change", "--code", "MISSING")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)


class QueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)

    def test_query_unknown_contract_fails(self) -> None:
        result = self.cli.query("NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_query_lists_fields_and_changes_in_registration_order(self) -> None:
        self.cli.register_contract(
            code="ORD", customer="客户甲", signed="2026-03-08", amount="1000"
        )
        self.cli.register_change(contract="ORD", code="A", description="一", delta="100")
        self.cli.register_change(contract="ORD", code="B", description="二", delta="-50")
        self.cli.register_change(contract="ORD", code="C", description="三", delta="25")
        self.cli.invoke("effect-change", "--code", "A")
        self.cli.invoke("effect-change", "--code", "B")
        # C 保持草稿；当前金额 = 1000 + 100 - 50 = 1050。
        result = self.cli.query("ORD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("客户名称：客户甲", result.stdout)
        self.assertIn("签订日期：2026-03-08", result.stdout)
        self.assertIn("初始金额：1000", result.stdout)
        self.assertIn("当前金额：1050", result.stdout)
        # 按登记先后排列。
        self.assertLess(result.stdout.index("- A"), result.stdout.index("- B"))
        self.assertLess(result.stdout.index("- B"), result.stdout.index("- C"))
        self.assertIn("变动：100", result.stdout)
        self.assertIn("变动：-50", result.stdout)
        self.assertIn("变动：25", result.stdout)

    def test_query_does_not_modify_data(self) -> None:
        self.cli.register_contract(amount="100")
        before = self.cli.query().stdout
        after = self.cli.query().stdout
        self.assertEqual(before, after)


class LifecyclePersistenceTests(unittest.TestCase):
    """跨进程：完整流转后在新进程中仍能读到全部状态。"""

    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)

    def test_full_lifecycle_persists(self) -> None:
        c = self.cli.register_contract(code="LIFE", amount="1000")
        self.assertEqual(c.returncode, 0, c.stderr)
        self.cli.register_change(contract="LIFE", code="L1", delta="500")
        self.cli.register_change(contract="LIFE", code="L2", delta="-200")
        self.cli.invoke("effect-change", "--code", "L1")
        self.cli.invoke("effect-change", "--code", "L2")
        self.cli.invoke("void-change", "--code", "L1")  # 余额 1300-500=800
        # 全新进程读取。
        result = self.cli.query("LIFE")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("初始金额：1000", result.stdout)
        self.assertIn("当前金额：800", result.stdout)
        self.assertIn("已作废", result.stdout)
        self.assertIn("已生效", result.stdout)


class MilestoneRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)

    def test_register_success_outputs_code(self) -> None:
        result = self.cli.register_milestone()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MS01", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_duplicate_milestone_fails_and_keeps_data(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        second = self.cli.register_milestone(name="另一个", amount="1")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("MS01", second.stderr)
        query = self.cli.query()
        self.assertIn("名称：首付款", query.stdout)
        self.assertNotIn("另一个", query.stdout)

    def test_register_on_nonexistent_contract_fails(self) -> None:
        result = self.cli.register_milestone(contract="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_invalid_amount_or_blank_name_fails(self) -> None:
        for bad in ["0", "-1", "1.234", "abc"]:
            with self.subTest(bad=bad):
                result = self.cli.register_milestone(code=f"M-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)
        result = self.cli.register_milestone(code="M-BLANK", name="  ")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("里程碑名称", result.stderr)

    def test_due_before_signed_date_fails(self) -> None:
        result = self.cli.register_milestone(due="2026-01-14")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("签订日期", result.stderr)
        self.assertIn("里程碑：无", self.cli.query().stdout)

    def test_invalid_due_format_fails(self) -> None:
        for bad in ["2026-13-01", "2026/06/30", "2026-6-30"]:
            with self.subTest(bad=bad):
                result = self.cli.register_milestone(code=f"D-{bad}", due=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("到期日", result.stderr)


class MilestoneDueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)

    def test_update_due_success(self) -> None:
        result = self.cli.set_milestone_due(due="2026-07-31")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MS01", result.stdout)
        self.assertIn("2026-07-31", result.stdout)
        self.assertIn("到期日：2026-07-31", self.cli.query().stdout)

    def test_unknown_milestone_fails(self) -> None:
        result = self.cli.set_milestone_due(code="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_due_before_signed_date_fails_and_keeps_old(self) -> None:
        result = self.cli.set_milestone_due(due="2026-01-01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("签订日期", result.stderr)
        self.assertIn("到期日：2026-06-30", self.cli.query().stdout)

    def test_due_before_latest_effective_change_fails(self) -> None:
        # 变更生效日期为今天；新到期日不得早于它。
        self.cli.register_change(code="C-EFF", delta="100", milestone="MS01")
        self.assertEqual(
            self.cli.invoke("effect-change", "--code", "C-EFF").returncode, 0
        )
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        result = self.cli.set_milestone_due(due=yesterday)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("生效日期", result.stderr)
        self.assertIn("到期日：2026-06-30", self.cli.query().stdout)


class MilestoneChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)

    def test_change_with_milestone_listed_with_owner(self) -> None:
        result = self.cli.register_change(code="CM1", delta="500", milestone="MS01")
        self.assertEqual(result.returncode, 0, result.stderr)
        query = self.cli.query()
        self.assertIn("归属：MS01", query.stdout)

    def test_change_without_milestone_keeps_contract_owner(self) -> None:
        self.assertEqual(self.cli.register_change(delta="100").returncode, 0)
        self.assertIn("归属：合同", self.cli.query().stdout)

    def test_unknown_milestone_rejected(self) -> None:
        result = self.cli.register_change(code="CX", delta="10", milestone="NOPE")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_milestone_of_other_contract_rejected(self) -> None:
        self.cli.register_contract(code="C002", customer="乙", amount="100")
        result = self.cli.register_change(
            contract="C002", code="CX", delta="10", milestone="MS01"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不属于", result.stderr)

    def test_effect_rejected_when_milestone_amount_not_positive(self) -> None:
        # 里程碑初始 3000：-3000 生效后里程碑金额为零，必须拒绝。
        self.cli.register_change(code="BIG", delta="-3000", milestone="MS01")
        result = self.cli.invoke("effect-change", "--code", "BIG")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("小于等于零", result.stderr)
        query = self.cli.query()
        self.assertIn("当前金额：3000", query.stdout)  # 里程碑金额不变
        self.assertIn("当前金额：10000", query.stdout)  # 合同金额不变

    def test_effect_allowed_when_milestone_stays_positive(self) -> None:
        self.cli.register_change(code="OK", delta="-2999.99", milestone="MS01")
        result = self.cli.invoke("effect-change", "--code", "OK")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("当前金额：0.01", self.cli.query().stdout)

    def test_void_draft_milestone_change_skips_check(self) -> None:
        self.cli.register_change(code="VD", delta="-99999", milestone="MS01")
        result = self.cli.invoke("void-change", "--code", "VD")
        self.assertEqual(result.returncode, 0, result.stderr)


class MilestoneQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)

    def test_no_milestone_outputs_none(self) -> None:
        self.cli.register_contract()
        result = self.cli.query()
        self.assertIn("里程碑：无", result.stdout)

    def test_milestones_listed_in_registration_order_with_current_amount(self) -> None:
        self.cli.register_contract()
        self.cli.register_milestone(code="MA", name="一期", amount="1000",
                                    due="2026-03-01")
        self.cli.register_milestone(code="MB", name="二期", amount="2000.50",
                                    due="2026-09-01")
        self.cli.register_change(code="CA", delta="-100", milestone="MA")
        self.cli.invoke("effect-change", "--code", "CA")
        result = self.cli.query()
        self.assertLess(result.stdout.index("- MA"), result.stdout.index("- MB"))
        self.assertIn("MA  名称：一期  到期日：2026-03-01  当前金额：900",
                      result.stdout)
        self.assertIn("MB  名称：二期  到期日：2026-09-01  当前金额：2000.5",
                      result.stdout)


if __name__ == "__main__":
    unittest.main()
