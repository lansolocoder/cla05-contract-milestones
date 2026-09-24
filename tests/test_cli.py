"""Checks for the documented command-line entry point."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CommandLineTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "contract_ledger", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_help_and_no_arguments(self) -> None:
        for arguments in [(), ("--help",)]:
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--help", result.stdout)
                self.assertIn("--version", result.stdout)
                self.assertEqual(result.stderr, "")

    def test_version(self) -> None:
        result = self.invoke("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "contract-ledger 0.1.0")
        self.assertEqual(result.stderr, "")

    def test_unknown_argument_is_an_error(self) -> None:
        result = self.invoke("--unknown-option")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--unknown-option", result.stderr)
        self.assertEqual(result.stdout, "")


class LedgerTests(unittest.TestCase):
    """Business operations against a temporary database file."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "ledger.db"
        self.env = dict(os.environ, CONTRACT_LEDGER_DB=str(self.db_path))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "contract_ledger", *arguments],
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def register_contract(self, contract_id: str = "C-001", amount: str = "1000.00"):
        return self.invoke(
            "register-contract",
            "--contract-id", contract_id,
            "--customer", "客户甲",
            "--date", "2026-09-01",
            "--amount", amount,
        )

    def register_change(
        self,
        change_id: str = "CH-1",
        contract_id: str = "C-001",
        delta: str = "200.00",
    ):
        return self.invoke(
            "register-change",
            "--contract-id", contract_id,
            "--change-id", change_id,
            "--description", "追加需求",
            "--delta", delta,
        )

    def current_amount(self, contract_id: str = "C-001") -> str:
        result = self.invoke("show-contract", "--contract-id", contract_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in result.stdout.splitlines():
            if line.startswith("当前金额:"):
                return line.split(":", 1)[1].strip()
        self.fail("查询输出缺少当前金额")

    def test_register_contract_and_persistence_across_processes(self) -> None:
        result = self.register_contract()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("C-001", result.stdout)
        self.assertIn("1000.00", result.stdout)
        self.assertTrue(self.db_path.exists())
        # 另起进程查询，数据仍在
        self.assertEqual(self.current_amount(), "1000.00 元")

    def test_duplicate_contract_rejected(self) -> None:
        self.assertEqual(self.register_contract().returncode, 0)
        result = self.register_contract()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C-001", result.stderr)
        self.assertEqual(self.current_amount(), "1000.00 元")

    def test_invalid_amount_and_date_rejected(self) -> None:
        for amount in ["abc", "1.234", "-5", "0", "0.00"]:
            with self.subTest(amount=amount):
                result = self.register_contract(amount=amount)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotEqual(result.stderr, "")
        result = self.invoke(
            "register-contract",
            "--contract-id", "C-002",
            "--customer", "客户乙",
            "--date", "2026-13-40",
            "--amount", "10",
        )
        self.assertNotEqual(result.returncode, 0)
        # 全部失败，未留下任何合同
        result = self.invoke("show-contract", "--contract-id", "C-002")
        self.assertNotEqual(result.returncode, 0)

    def test_change_lifecycle(self) -> None:
        self.assertEqual(self.register_contract().returncode, 0)
        self.assertEqual(self.register_change().returncode, 0)
        # 草稿不计入当前金额
        self.assertEqual(self.current_amount(), "1000.00 元")
        # 生效后计入
        result = self.invoke("apply-change", "--change-id", "CH-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.current_amount(), "1200.00 元")
        # 重复生效被拒绝
        result = self.invoke("apply-change", "--change-id", "CH-1")
        self.assertNotEqual(result.returncode, 0)
        # 作废已生效变更单，金额回退
        result = self.invoke("void-change", "--change-id", "CH-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.current_amount(), "1000.00 元")
        # 作废后不得再生效、不得再作废
        for sub in ("apply-change", "void-change"):
            result = self.invoke(sub, "--change-id", "CH-1")
            self.assertNotEqual(result.returncode, 0)
        # 已作废记录仍保留可查
        result = self.invoke("show-contract", "--contract-id", "C-001")
        self.assertIn("CH-1", result.stdout)
        self.assertIn("已作废", result.stdout)

    def test_void_draft_change_keeps_amount(self) -> None:
        self.assertEqual(self.register_contract().returncode, 0)
        self.assertEqual(self.register_change().returncode, 0)
        result = self.invoke("void-change", "--change-id", "CH-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.current_amount(), "1000.00 元")

    def test_apply_rejected_when_amount_not_positive(self) -> None:
        self.assertEqual(self.register_contract().returncode, 0)
        self.assertEqual(
            self.register_change(change_id="CH-NEG", delta="-1000.00").returncode, 0
        )
        result = self.invoke("apply-change", "--change-id", "CH-NEG")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(result.stderr, "")
        # 状态与金额保持原样
        self.assertEqual(self.current_amount(), "1000.00 元")
        result = self.invoke("show-contract", "--contract-id", "C-001")
        self.assertIn("草稿", result.stdout)

    def test_void_applied_rejected_when_amount_not_positive(self) -> None:
        self.assertEqual(self.register_contract().returncode, 0)
        self.assertEqual(
            self.register_change(change_id="CH-UP", delta="500.00").returncode, 0
        )
        self.assertEqual(
            self.register_change(change_id="CH-DOWN", delta="-1400.00").returncode, 0
        )
        self.assertEqual(self.invoke("apply-change", "--change-id", "CH-UP").returncode, 0)
        self.assertEqual(self.invoke("apply-change", "--change-id", "CH-DOWN").returncode, 0)
        self.assertEqual(self.current_amount(), "100.00 元")
        # 作废 CH-UP 会使当前金额变为 -400，拒绝
        result = self.invoke("void-change", "--change-id", "CH-UP")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.current_amount(), "100.00 元")
        result = self.invoke("show-contract", "--contract-id", "C-001")
        self.assertIn("已生效", result.stdout)

    def test_invalid_change_registration(self) -> None:
        self.assertEqual(self.register_contract().returncode, 0)
        # 引用不存在的合同
        result = self.register_change(contract_id="C-404")
        self.assertNotEqual(result.returncode, 0)
        # 零变动、非法格式、空说明
        result = self.register_change(delta="0.00")
        self.assertNotEqual(result.returncode, 0)
        result = self.register_change(delta="1.005")
        self.assertNotEqual(result.returncode, 0)
        result = self.invoke(
            "register-change",
            "--contract-id", "C-001",
            "--change-id", "CH-X",
            "--description", "   ",
            "--delta", "10",
        )
        self.assertNotEqual(result.returncode, 0)
        # 重复变更单编号
        self.assertEqual(self.register_change().returncode, 0)
        result = self.register_change()
        self.assertNotEqual(result.returncode, 0)
        # 以上失败均未留下变更单（只有 CH-1 一条草稿）
        result = self.invoke("show-contract", "--contract-id", "C-001")
        self.assertEqual(result.stdout.count("CH-"), 1, result.stdout)

    def test_query_unknown_contract_fails(self) -> None:
        result = self.invoke("show-contract", "--contract-id", "C-404")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C-404", result.stderr)

    def test_changes_listed_in_registration_order(self) -> None:
        self.assertEqual(self.register_contract().returncode, 0)
        for change_id in ("CH-B", "CH-A", "CH-C"):
            self.assertEqual(self.register_change(change_id=change_id).returncode, 0)
        result = self.invoke("show-contract", "--contract-id", "C-001")
        positions = [result.stdout.index(cid) for cid in ("CH-B", "CH-A", "CH-C")]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
