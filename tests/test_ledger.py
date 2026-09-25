"""合同登记与变更管理的端到端 CLI 测试。

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

    def register_change(self, contract="C001", code="CHG01",
                        description="追加需求", delta="2000"):
        return self.invoke(
            "register-change",
            "--contract", contract, "--code", code,
            "--description", description, "--delta", delta,
        )

    def register_milestone(self, contract="C001", code="M01", title="首付款",
                           amount="3000", due="2026-03-01"):
        return self.invoke(
            "register-milestone",
            "--contract", contract, "--code", code,
            "--title", title, "--amount", amount, "--due", due,
        )

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
        self.assertEqual(self.cli.register_contract(amount="10000").returncode, 0)

    def test_register_success_is_pending_and_lists_in_query(self) -> None:
        result = self.cli.register_milestone(code="M1", amount="3000.50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("M1", result.stdout)
        self.assertIn("3000.5", result.stdout)
        query = self.cli.query()
        self.assertIn("里程碑：", query.stdout)
        self.assertIn("M1", query.stdout)
        self.assertIn("待确认", query.stdout)
        # 待确认不计入已确认金额。
        self.assertIn("已确认金额：0", query.stdout)

    def test_multiple_milestones_listed_in_registration_order(self) -> None:
        self.cli.register_milestone(code="M3", due="2026-03-01")
        self.cli.register_milestone(code="M1", due="2026-04-01")
        self.cli.register_milestone(code="M2", due="2026-05-01")
        out = self.cli.query().stdout
        self.assertLess(out.index("- M3"), out.index("- M1"))
        self.assertLess(out.index("- M1"), out.index("- M2"))

    def test_register_on_nonexistent_contract_fails(self) -> None:
        result = self.cli.register_milestone(contract="NOPE", code="X1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_duplicate_code_fails_globally(self) -> None:
        self.assertEqual(self.cli.register_milestone(code="DUP").returncode, 0)
        self.cli.register_contract(code="C002", customer="乙", amount="100")
        result = self.cli.register_milestone(contract="C002", code="DUP")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DUP", result.stderr)
        # 第二份合同没有留下任何里程碑。
        self.assertIn("里程碑：无", self.cli.query("C002").stdout)

    def test_blank_title_rejected(self) -> None:
        result = self.cli.register_milestone(code="B1", title="   ")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("里程碑标题", result.stderr)

    def test_invalid_amounts_fail(self) -> None:
        for bad in ["0", "-1", "1.234", "abc", "", " 100"]:
            with self.subTest(bad=bad):
                result = self.cli.register_milestone(code=f"A-{bad or 'e'}", amount=bad)
                self.assertNotEqual(result.returncode, 0, f"应当拒绝 {bad!r}")
                self.assertIn("金额", result.stderr)

    def test_invalid_due_date_fails(self) -> None:
        for bad in ["2026-13-01", "2026-02-30", "2026/03/01", "20260301", "2026-3-1"]:
            with self.subTest(bad=bad):
                result = self.cli.register_milestone(code=f"D-{bad}", due=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("到期日", result.stderr)

    def test_failed_registration_changes_nothing(self) -> None:
        # 非法日期失败后，合同仍无里程碑。
        bad = self.cli.register_milestone(code="G1", due="2026-02-30")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("里程碑：无", self.cli.query().stdout)


class MilestoneScalingTests(unittest.TestCase):
    """里程碑当前金额随合同变更联动缩放。"""

    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        # 初始 3 元，便于验证向下取整到分。
        self.assertEqual(
            self.cli.register_contract(code="C001", amount="3").returncode, 0
        )
        self.cli.register_milestone(code="MA", amount="1", due="2026-02-01")
        self.cli.register_milestone(code="MB", amount="2", due="2026-03-01")
        self.assertEqual(self.cli.invoke("confirm-milestone", "--code", "MA").returncode, 0)

    def change(self, code, delta, *, effect=True):
        self.assertEqual(
            self.cli.register_change(code=code, delta=delta).returncode, 0
        )
        if effect:
            self.assertEqual(
                self.cli.invoke("effect-change", "--code", code).returncode, 0
            )

    def test_scale_down_floors_to_cent(self) -> None:
        # 3 -> 2：MA = 100*200//300 = 66 分 = 0.66；
        # MB = 200*200//300 = 133 分 = 1.33。
        self.change("D1", "-1")
        out = self.cli.query().stdout
        self.assertIn("当前金额：0.66", out)
        self.assertIn("当前金额：1.33", out)
        # 只有已确认的 MA 计入。
        self.assertIn("已确认金额：0.66", out)

    def test_scale_up(self) -> None:
        # 3 -> 6：MA = 2，MB = 4。
        self.change("D1", "3")
        out = self.cli.query().stdout
        self.assertIn("当前金额：2", out)
        self.assertIn("当前金额：4", out)
        self.assertIn("已确认金额：2", out)

    def test_voiding_effective_change_restores_amounts(self) -> None:
        self.change("D1", "-1")
        self.assertEqual(self.cli.invoke("void-change", "--code", "D1").returncode, 0)
        out = self.cli.query().stdout
        # 回到 3：MA = 1，MB = 2。
        self.assertIn("当前金额：1", out)
        self.assertIn("当前金额：2", out)
        self.assertIn("已确认金额：1", out)

    def test_draft_change_does_not_scale(self) -> None:
        # 草稿不影响合同当前金额，里程碑金额不变。
        self.change("D1", "999", effect=False)
        out = self.cli.query().stdout
        self.assertIn("当前金额：1", out)
        self.assertIn("当前金额：2", out)

    def test_query_milestone_reflects_scaled_amount(self) -> None:
        self.change("D1", "3")  # 3 -> 6
        result = self.cli.invoke("query-milestone", "--code", "MA")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("所属合同编号：C001", result.stdout)
        self.assertIn("登记金额：1", result.stdout)
        self.assertIn("当前金额：2", result.stdout)
        self.assertIn("到期日：2026-02-01", result.stdout)
        self.assertIn("已确认", result.stdout)


class MilestoneLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract(amount="100").returncode, 0)
        self.cli.register_milestone(code="M1", amount="40", due="2026-02-01")
        self.cli.register_milestone(code="M2", amount="60", due="2026-03-01")

    def test_confirm_then_cancel_flow_and_confirmed_total(self) -> None:
        self.assertEqual(
            self.cli.invoke("confirm-milestone", "--code", "M1").returncode, 0
        )
        out = self.cli.query().stdout
        self.assertIn("已确认金额：40", out)
        # 第二个确认后累加。
        self.assertEqual(
            self.cli.invoke("confirm-milestone", "--code", "M2").returncode, 0
        )
        self.assertIn("已确认金额：100", self.cli.query().stdout)
        # 作废 M1 后只剩 M2。
        self.assertEqual(
            self.cli.invoke("cancel-milestone", "--code", "M1").returncode, 0
        )
        self.assertIn("已确认金额：60", self.cli.query().stdout)
        self.assertIn("已作废", self.cli.query().stdout)

    def test_pending_can_cancel_directly(self) -> None:
        result = self.cli.invoke("cancel-milestone", "--code", "M1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("已作废", self.cli.query().stdout)
        # 已作废不计入已确认金额。
        self.assertIn("已确认金额：0", self.cli.query().stdout)

    def test_cannot_confirm_twice(self) -> None:
        self.cli.invoke("confirm-milestone", "--code", "M1")
        again = self.cli.invoke("confirm-milestone", "--code", "M1")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("已确认", again.stderr)

    def test_canceled_is_terminal(self) -> None:
        self.cli.invoke("cancel-milestone", "--code", "M1")
        confirm = self.cli.invoke("confirm-milestone", "--code", "M1")
        self.assertNotEqual(confirm.returncode, 0)
        self.assertIn("作废", confirm.stderr)
        cancel = self.cli.invoke("cancel-milestone", "--code", "M1")
        self.assertNotEqual(cancel.returncode, 0)
        self.assertIn("作废", cancel.stderr)

    def test_confirm_unknown_milestone_fails(self) -> None:
        result = self.cli.invoke("confirm-milestone", "--code", "GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_cancel_unknown_milestone_fails(self) -> None:
        result = self.cli.invoke("cancel-milestone", "--code", "GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_query_unknown_milestone_fails_nonzero_stderr(self) -> None:
        result = self.cli.invoke("query-milestone", "--code", "GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_failed_transition_changes_nothing(self) -> None:
        self.cli.invoke("confirm-milestone", "--code", "M1")
        # 重复确认失败：M1 仍为已确认，M2 仍为待确认，已确认金额仍为 40。
        bad = self.cli.invoke("confirm-milestone", "--code", "M1")
        self.assertNotEqual(bad.returncode, 0)
        out = self.cli.query().stdout
        self.assertIn("M1", out)
        self.assertIn("M2", out)
        self.assertIn("待确认", out)
        self.assertIn("已确认金额：40", out)
        # M1 行仍为已确认（该行不含"已确认金额"前缀）。
        m1_line = next(line for line in out.splitlines() if "- M1" in line)
        self.assertIn("状态：已确认", m1_line)


class MilestoneQueryOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)

    def test_no_milestones_shows_none_and_zero_confirmed(self) -> None:
        self.cli.register_contract(amount="100")
        out = self.cli.query().stdout
        self.assertIn("里程碑：无", out)
        self.assertIn("已确认金额：0", out)


if __name__ == "__main__":
    unittest.main()
