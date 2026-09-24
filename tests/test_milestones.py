"""付款里程碑及其变更单的端到端 CLI 测试。

与 test_ledger.py 相同：每个用例使用独立临时数据库
（CONTRACT_LEDGER_DB），测试结束后清理。
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
TODAY = date.today().isoformat()
TOMORROW = (date.today() + timedelta(days=1)).isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


class CLIHarness:
    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ledger-milestone-test-")
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

    def void(self, code):
        return self.invoke("void-change", "--code", code)

    def set_due(self, code, due):
        return self.invoke("set-milestone-due", "--code", code, "--due", due)

    def query(self, code="C001"):
        return self.invoke("query-contract", "--code", code)


class MilestoneRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)

    def test_register_success_outputs_code(self) -> None:
        result = self.cli.register_milestone(amount="4000.50", due="2026-02-01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("里程碑编号：MS1", result.stdout)

    def test_duplicate_code_rejected_globally(self) -> None:
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.cli.register_contract(code="C002", customer="乙", amount="100")
        second = self.cli.register_milestone(
            contract="C002", code="MS1", name="另一个", due="2026-03-01"
        )
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("MS1", second.stderr)
        # 第二份合同没有留下任何里程碑。
        self.assertIn("里程碑：无", self.cli.query("C002").stdout)

    def test_duplicate_keeps_existing_data(self) -> None:
        self.assertEqual(
            self.cli.register_milestone(name="原名", amount="4000",
                                        due="2026-02-01").returncode,
            0,
        )
        bad = self.cli.register_milestone(
            code="MS1", name="改名", amount="999", due="2026-03-01"
        )
        self.assertNotEqual(bad.returncode, 0)
        query = self.cli.query().stdout
        self.assertIn("名称：原名", query)
        self.assertIn("到期日：2026-02-01", query)
        self.assertIn("当前金额：4000", query)
        self.assertNotIn("改名", query)

    def test_blank_name_rejected(self) -> None:
        result = self.cli.register_milestone(name="   ")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("里程碑名称", result.stderr)

    def test_non_positive_amount_rejected(self) -> None:
        for bad in ["0", "-1", "0.00"]:
            with self.subTest(bad=bad):
                result = self.cli.register_milestone(code=f"A-{bad}", amount=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("金额", result.stderr)

    def test_amount_too_many_decimals_rejected(self) -> None:
        result = self.cli.register_milestone(code="A1", amount="1.005")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("金额", result.stderr)

    def test_invalid_due_date_rejected(self) -> None:
        for bad in ["2026-13-01", "2026/02/01", "20260201", "2026-2-1"]:
            with self.subTest(bad=bad):
                result = self.cli.register_milestone(code=f"D-{bad}", due=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("到期日", result.stderr)

    def test_due_before_signed_date_rejected(self) -> None:
        result = self.cli.register_milestone(due="2026-01-14")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("签订日期", result.stderr)
        self.assertIn("里程碑：无", self.cli.query().stdout)

    def test_due_equal_to_signed_date_allowed(self) -> None:
        result = self.cli.register_milestone(due="2026-01-15")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_contract_rejected(self) -> None:
        result = self.cli.register_milestone(contract="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)


class SetMilestoneDueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone(due="2026-02-01").returncode, 0)

    def test_set_due_success(self) -> None:
        result = self.cli.set_due("MS1", "2026-05-01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("里程碑编号：MS1", result.stdout)
        self.assertIn("新到期日：2026-05-01", result.stdout)
        self.assertIn("到期日：2026-05-01", self.cli.query().stdout)

    def test_unknown_milestone_rejected(self) -> None:
        result = self.cli.set_due("NOPE", "2026-05-01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)

    def test_invalid_date_rejected(self) -> None:
        result = self.cli.set_due("MS1", "2026-02-30")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("到期日", result.stderr)
        self.assertIn("到期日：2026-02-01", self.cli.query().stdout)

    def test_due_before_signed_date_rejected(self) -> None:
        result = self.cli.set_due("MS1", "2026-01-01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("签订日期", result.stderr)
        self.assertIn("到期日：2026-02-01", self.cli.query().stdout)

    def test_due_before_effective_change_date_rejected(self) -> None:
        # 生效日期为运行当天；新到期日早于它但晚于签订日期。
        self.assertEqual(
            self.cli.register_change(code="CH1", delta="-100", milestone="MS1").returncode,
            0,
        )
        self.assertEqual(self.cli.effect("CH1").returncode, 0)
        result = self.cli.set_due("MS1", YESTERDAY)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("最晚生效日期", result.stderr)
        # 原到期日保持不变。
        self.assertIn("到期日：2026-02-01", self.cli.query().stdout)

    def test_due_equal_to_effective_change_date_allowed(self) -> None:
        self.assertEqual(
            self.cli.register_change(code="CH1", delta="-100", milestone="MS1").returncode,
            0,
        )
        self.assertEqual(self.cli.effect("CH1").returncode, 0)
        result = self.cli.set_due("MS1", TODAY)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_due_after_effective_change_date_allowed(self) -> None:
        self.assertEqual(
            self.cli.register_change(code="CH1", delta="-100", milestone="MS1").returncode,
            0,
        )
        self.assertEqual(self.cli.effect("CH1").returncode, 0)
        result = self.cli.set_due("MS1", TOMORROW)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_void_and_draft_changes_do_not_bind_due(self) -> None:
        # 一张草稿、一张已作废：都不参与最晚生效日期比较。
        self.assertEqual(
            self.cli.register_change(code="D1", delta="-100", milestone="MS1").returncode,
            0,
        )
        self.assertEqual(
            self.cli.register_change(code="V1", delta="-100", milestone="MS1").returncode,
            0,
        )
        self.assertEqual(self.cli.void("V1").returncode, 0)
        # 无任何已生效未作废变更，只要不早于签订日期即可。
        result = self.cli.set_due("MS1", "2026-01-15")
        self.assertEqual(result.returncode, 0, result.stderr)


class MilestoneChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract(amount="10000").returncode, 0)
        self.assertEqual(
            self.cli.register_milestone(amount="4000", due=TOMORROW).returncode, 0
        )

    def test_unknown_milestone_rejected(self) -> None:
        result = self.cli.register_change(code="X1", milestone="GHOST")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("里程碑编号不存在", result.stderr)
        self.assertIn("变更单：无", self.cli.query().stdout)

    def test_milestone_of_other_contract_rejected(self) -> None:
        self.cli.register_contract(code="C002", customer="乙", amount="100")
        result = self.cli.register_change(
            contract="C002", code="X1", milestone="MS1"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不属于", result.stderr)
        self.assertIn("变更单：无", self.cli.query("C002").stdout)

    def test_draft_milestone_change_affects_nothing(self) -> None:
        result = self.cli.register_change(code="X1", delta="-1000", milestone="MS1")
        self.assertEqual(result.returncode, 0, result.stderr)
        query = self.cli.query().stdout
        self.assertIn("当前金额：10000", query)  # 合同金额不变
        self.assertIn("当前金额：4000", query)   # 里程碑金额不变
        self.assertIn("草稿", query)
        self.assertIn("归属：MS1", query)

    def test_effective_milestone_change_updates_both_amounts(self) -> None:
        self.assertEqual(
            self.cli.register_change(code="X1", delta="-1000", milestone="MS1").returncode,
            0,
        )
        result = self.cli.effect("X1")
        self.assertEqual(result.returncode, 0, result.stderr)
        query = self.cli.query().stdout
        self.assertIn("当前金额：9000", query)
        self.assertIn("当前金额：3000", query)

    def test_effect_rejected_when_milestone_becomes_zero(self) -> None:
        self.assertEqual(
            self.cli.register_change(code="X1", delta="-4000", milestone="MS1").returncode,
            0,
        )
        result = self.cli.effect("X1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("里程碑当前金额将小于等于零", result.stderr)
        query = self.cli.query().stdout
        self.assertIn("当前金额：10000", query)
        self.assertIn("当前金额：4000", query)
        self.assertIn("草稿", query)

    def test_effect_rejected_when_milestone_becomes_negative(self) -> None:
        self.assertEqual(
            self.cli.register_change(code="X1", delta="-5000", milestone="MS1").returncode,
            0,
        )
        result = self.cli.effect("X1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("里程碑当前金额将小于等于零", result.stderr)
        query = self.cli.query().stdout
        self.assertIn("当前金额：4000", query)

    def test_milestone_check_uses_sum_of_effective_changes(self) -> None:
        # -3000 生效后里程碑余 1000；再来 -1000 恰好为零，必须拒绝。
        self.assertEqual(
            self.cli.register_change(code="A", delta="-3000", milestone="MS1").returncode,
            0,
        )
        self.assertEqual(self.cli.effect("A").returncode, 0)
        self.assertEqual(
            self.cli.register_change(code="B", delta="-1000", milestone="MS1").returncode,
            0,
        )
        self.assertNotEqual(self.cli.effect("B").returncode, 0)
        # -999 则余额 1，允许生效。
        self.assertEqual(
            self.cli.register_change(code="C", delta="-999", milestone="MS1").returncode,
            0,
        )
        self.assertEqual(self.cli.effect("C").returncode, 0)
        self.assertIn("当前金额：1", self.cli.query().stdout)

    def test_voided_milestone_change_excluded_from_sum(self) -> None:
        # -3000 生效后作废；合计回到 0，-4000 单张将使里程碑为零 -> 拒绝边界
        # 对照：-3999 应允许。
        self.assertEqual(
            self.cli.register_change(code="A", delta="-3000", milestone="MS1").returncode,
            0,
        )
        self.assertEqual(self.cli.effect("A").returncode, 0)
        self.assertEqual(self.cli.void("A").returncode, 0)
        self.assertIn("当前金额：4000", self.cli.query().stdout)
        self.assertEqual(
            self.cli.register_change(code="B", delta="-4000", milestone="MS1").returncode,
            0,
        )
        self.assertNotEqual(self.cli.effect("B").returncode, 0)
        self.assertEqual(
            self.cli.register_change(code="C", delta="-3999", milestone="MS1").returncode,
            0,
        )
        self.assertEqual(self.cli.effect("C").returncode, 0)
        self.assertIn("当前金额：1", self.cli.query().stdout)

    def test_void_draft_milestone_change_skips_check(self) -> None:
        # 草稿作废即使金额巨大也不做任何余额校验。
        self.assertEqual(
            self.cli.register_change(code="X1", delta="-999999", milestone="MS1").returncode,
            0,
        )
        result = self.cli.void("X1")
        self.assertEqual(result.returncode, 0, result.stderr)
        query = self.cli.query().stdout
        self.assertIn("当前金额：10000", query)
        self.assertIn("当前金额：4000", query)
        self.assertIn("已作废", query)

    def test_milestone_change_still_subject_to_contract_check(self) -> None:
        # 合同金额 10000、里程碑 4000；-10000 的里程碑变更虽不把里程碑
        # 压到零以下的组合，但合同金额先变零，合同级校验依旧拦截。
        self.cli.register_contract(code="C2", customer="乙", signed="2026-01-15",
                                  amount="100")
        self.assertEqual(
            self.cli.register_milestone(
                contract="C2", code="MS2", amount="100", due=TOMORROW
            ).returncode,
            0,
        )
        self.assertEqual(
            self.cli.register_change(
                contract="C2", code="Z", delta="-100", milestone="MS2"
            ).returncode,
            0,
        )
        result = self.cli.effect("Z")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("合同当前金额将小于等于零", result.stderr)


class MilestoneQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.cli.register_contract(code="ORD", customer="客户甲",
                                  signed="2026-03-08", amount="1000")

    def test_no_milestones_shows_none(self) -> None:
        result = self.cli.query("ORD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("里程碑：无", result.stdout)

    def test_milestones_listed_in_registration_order(self) -> None:
        self.cli.register_milestone(contract="ORD", code="M1", name="首款",
                                   amount="300", due="2026-04-01")
        self.cli.register_milestone(contract="ORD", code="M2", name="尾款",
                                   amount="700", due="2026-05-01")
        result = self.cli.query("ORD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("里程碑：", result.stdout)
        self.assertLess(result.stdout.index("- M1"), result.stdout.index("- M2"))
        self.assertIn(
            "- M1  名称：首款  到期日：2026-04-01  当前金额：300",
            result.stdout,
        )
        self.assertIn(
            "- M2  名称：尾款  到期日：2026-05-01  当前金额：700",
            result.stdout,
        )

    def test_change_attribution_labels(self) -> None:
        self.cli.register_milestone(contract="ORD", code="M1", name="首款",
                                   amount="300", due="2026-04-01")
        self.cli.register_change(contract="ORD", code="CA", description="合同级",
                                 delta="10")
        self.cli.register_change(contract="ORD", code="MB", description="里程碑级",
                                 delta="-20", milestone="M1")
        result = self.cli.query("ORD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("归属：合同", result.stdout)
        self.assertIn("归属：M1", result.stdout)

    def test_query_does_not_modify_data(self) -> None:
        self.cli.register_milestone(contract="ORD", code="M1", name="首款",
                                   amount="300", due="2026-04-01")
        before = self.cli.query("ORD").stdout
        after = self.cli.query("ORD").stdout
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
