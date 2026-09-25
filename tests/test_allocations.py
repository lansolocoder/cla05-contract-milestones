"""按合同登记分配收款（一笔实收款分配到多张发票）的端到端 CLI 测试。

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
        self.tmp = tempfile.mkdtemp(prefix="ledger-allocation-test-")
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
            "--contract", contract, "--code", code,
            "--name", name, "--amount", amount, "--due", due,
        )

    def invoice(self, contract="C001", milestone="MS01", code="INV01",
                amount="4000", date="2026-03-01"):
        return self.invoke(
            "invoice",
            "--contract", contract, "--milestone", milestone,
            "--code", code, "--amount", amount, "--date", date,
        )

    def receive(self, invoice="INV01", code="RCV01",
                amount="1000", date="2026-03-05"):
        return self.invoke(
            "receive",
            "--invoice", invoice, "--code", code,
            "--amount", amount, "--date", date,
        )

    def allocate(self, contract="C001", code="ALC01", date="2026-05-10",
                 allocations=()):
        arguments = [
            "receive-allocation",
            "--contract", contract, "--code", code, "--date", date,
        ]
        for invoice_code, amount in allocations:
            arguments += ["--allocation", invoice_code, amount]
        return self.invoke(*arguments)

    def query(self, code="C001"):
        return self.invoke("query-contract", "--code", code)


class AllocationFixture(unittest.TestCase):
    """两张发票：INV01 4000（2026-03-01）、INV02 6000（2026-05-01）。"""

    def setUp(self) -> None:
        self.cli = CLIHarness()
        self.addCleanup(self.cli.cleanup)
        self.assertEqual(self.cli.register_contract().returncode, 0)
        self.assertEqual(self.cli.register_milestone().returncode, 0)
        self.assertEqual(
            self.cli.register_milestone(
                code="MS02", name="尾款", amount="6000", due="2026-05-01"
            ).returncode,
            0,
        )
        self.assertEqual(self.cli.invoice().returncode, 0)
        self.assertEqual(
            self.cli.invoice(
                milestone="MS02", code="INV02", amount="6000", date="2026-05-01"
            ).returncode,
            0,
        )


class AllocationSuccessTests(AllocationFixture):
    def test_success_outputs_receipt_code_on_stdout(self) -> None:
        result = self.cli.allocate(
            allocations=[("INV01", "1500"), ("INV02", "3000")]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "收款流水号：ALC01")
        self.assertEqual(result.stderr, "")

    def test_single_allocation_allowed(self) -> None:
        result = self.cli.allocate(code="A1", allocations=[("INV02", "2999.99")])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "收款流水号：A1")
        query = self.cli.query()
        self.assertIn("已收金额：2999.99", query.stdout)

    def test_receipt_amount_equals_sum_of_allocations(self) -> None:
        result = self.cli.allocate(
            code="A1", allocations=[("INV01", "1500.50"), ("INV02", "250")]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        query = self.cli.query()
        # 收款金额 = 1500.50 + 250 = 1750.50（金额沿用现有金额格式）。
        self.assertIn("A1  发票：多张  金额：1750.50", query.stdout)
        self.assertIn("分配：INV01 1500.50 INV02 250", query.stdout)

    def test_invoice_received_includes_prior_and_allocated(self) -> None:
        # INV01 此前普通收款 500，本次再分配 1500 -> 已收 2000（部分收款）。
        self.assertEqual(
            self.cli.receive(code="OLD1", amount="500").returncode, 0
        )
        result = self.cli.allocate(
            code="A1", allocations=[("INV01", "1500"), ("INV02", "6000")]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        query = self.cli.query()
        self.assertIn("INV01", query.stdout)
        self.assertIn("已收金额：2000  状态：部分收款", query.stdout)
        # INV02 一次性收齐。
        self.assertIn("已收金额：6000  状态：已收齐", query.stdout)

    def test_allocation_can_finish_invoice(self) -> None:
        self.assertEqual(
            self.cli.receive(code="OLD1", invoice="INV01", amount="3000").returncode,
            0,
        )
        result = self.cli.allocate(code="A1", allocations=[("INV01", "1000")])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("已收齐", self.cli.query().stdout)

    def test_query_receipt_section_orders_and_details(self) -> None:
        self.assertEqual(
            self.cli.receive(code="R-OLD", invoice="INV02",
                             amount="200", date="2026-05-02").returncode,
            0,
        )
        self.assertEqual(
            self.cli.allocate(
                code="R-ALL", date="2026-05-10",
                allocations=[("INV02", "800"), ("INV01", "100")],
            ).returncode,
            0,
        )
        out = self.cli.query().stdout
        # 收款段按登记先后：普通收款在前。
        self.assertLess(out.index("R-OLD"), out.index("R-ALL"))
        # 普通收款追加“分配：整笔归 <发票>”。
        self.assertIn(
            "R-OLD  发票：INV02  金额：200  收款日：2026-05-02  分配：整笔归 INV02",
            out,
        )
        # 分配收款按输入先后列出各分配项。
        self.assertIn("分配：INV02 800 INV01 100", out)
        # 明细内顺序即输入顺序（INV02 在 INV01 之前）。
        detail = out[out.index("R-ALL"):]
        self.assertLess(detail.index("INV02 800"), detail.index("INV01 100"))

    def test_query_does_not_modify_data(self) -> None:
        self.assertEqual(
            self.cli.allocate(
                allocations=[("INV01", "1"), ("INV02", "2")]
            ).returncode,
            0,
        )
        before = self.cli.query().stdout
        after = self.cli.query().stdout
        self.assertEqual(before, after)


class AllocationRejectionTests(AllocationFixture):
    def _assert_rejected(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(result.stderr, "")
        # 全部校验失败：标准输出为空。
        self.assertEqual(result.stdout, "")

    def test_duplicate_receipt_code_against_normal_receipt(self) -> None:
        self.assertEqual(
            self.cli.receive(code="DUP", amount="10").returncode, 0
        )
        result = self.cli.allocate(
            code="DUP", allocations=[("INV01", "10")]
        )
        self._assert_rejected(result)
        self.assertIn("DUP", result.stderr)

    def test_duplicate_receipt_code_against_allocation(self) -> None:
        self.assertEqual(
            self.cli.allocate(code="DUP", allocations=[("INV01", "10")]).returncode,
            0,
        )
        result = self.cli.allocate(
            code="DUP", allocations=[("INV02", "10")]
        )
        self._assert_rejected(result)
        self.assertIn("DUP", result.stderr)
        # 第二笔未落库：INV02 仍未收款。
        self.assertIn("已收金额：0", self.cli.query().stdout)

    def test_date_must_not_precede_any_invoice_date(self) -> None:
        # INV01 开票 2026-03-01、INV02 开票 2026-05-01；4 月 30 日
        # 对 INV01 合法但早于 INV02，整体拒绝。
        result = self.cli.allocate(
            code="E1", date="2026-04-30",
            allocations=[("INV01", "100"), ("INV02", "100")],
        )
        self._assert_rejected(result)
        self.assertIn("2026-05-01", result.stderr)
        self.assertIn("收款日期不得早于开票日期", result.stderr)

    def test_duplicate_invoice_in_same_operation_rejected(self) -> None:
        result = self.cli.allocate(
            code="E1", allocations=[("INV01", "100"), ("INV01", "200")]
        )
        self._assert_rejected(result)
        self.assertIn("INV01", result.stderr)
        self.assertIn("不得重复", result.stderr)
        self.assertNotIn("E1", self.cli.query().stdout)

    def test_unknown_invoice_rejected(self) -> None:
        result = self.cli.allocate(
            code="E1", allocations=[("INV01", "100"), ("GHOST", "100")]
        )
        self._assert_rejected(result)
        self.assertIn("GHOST", result.stderr)
        # 第一条合法分配项同样不得落库。
        self.assertNotIn("E1", self.cli.query().stdout)
        self.assertIn("已收金额：0", self.cli.query().stdout)

    def test_invoice_of_other_contract_rejected(self) -> None:
        self.assertEqual(
            self.cli.register_contract(
                code="C002", customer="乙", signed="2026-01-01", amount="1000"
            ).returncode,
            0,
        )
        self.assertEqual(
            self.cli.register_milestone(
                contract="C002", code="MS03", name="他", amount="1000",
                due="2026-02-01",
            ).returncode,
            0,
        )
        self.assertEqual(
            self.cli.invoice(
                contract="C002", milestone="MS03", code="INV03",
                amount="1000", date="2026-02-01",
            ).returncode,
            0,
        )
        result = self.cli.allocate(
            code="E1", allocations=[("INV01", "100"), ("INV03", "100")]
        )
        self._assert_rejected(result)
        self.assertIn("INV03", result.stderr)
        self.assertIn("不属于合同", result.stderr)
        self.assertNotIn("E1", self.cli.query().stdout)

    def test_unknown_contract_rejected(self) -> None:
        result = self.cli.allocate(
            contract="NOPE", code="E1", allocations=[("INV01", "100")]
        )
        self._assert_rejected(result)
        self.assertIn("NOPE", result.stderr)

    def test_bad_allocation_amounts_rejected(self) -> None:
        for bad in ["0", "-1", "1.234", "abc", "0.00"]:
            with self.subTest(bad=bad):
                result = self.cli.allocate(
                    code=f"E-{bad or 'empty'}",
                    allocations=[("INV01", bad)],
                )
                self._assert_rejected(result)

    def test_cumulative_must_not_exceed_invoice_amount(self) -> None:
        self.assertEqual(
            self.cli.receive(code="OLD1", invoice="INV01",
                             amount="3500", date="2026-03-05").returncode,
            0,
        )
        # INV01 已收 3500，再分配 600 即超限（即便 INV02 的项合法）。
        result = self.cli.allocate(
            code="E1", allocations=[("INV02", "100"), ("INV01", "600")]
        )
        self._assert_rejected(result)
        self.assertIn("INV01", result.stderr)
        self.assertIn("超过发票金额", result.stderr)
        query = self.cli.query()
        # 整笔失败：收款记录不存在，INV02 未收款，INV01 仍为 3500。
        self.assertNotIn("E1", query.stdout)
        self.assertIn("已收金额：3500", query.stdout)

    def test_cumulative_includes_prior_allocations(self) -> None:
        self.assertEqual(
            self.cli.allocate(
                code="A1", allocations=[("INV01", "3000")]
            ).returncode,
            0,
        )
        result = self.cli.allocate(
            code="A2", allocations=[("INV01", "1000.01")]
        )
        self._assert_rejected(result)
        # INV01 已收仍为 3000（部分收款），A2 未落库。
        query = self.cli.query()
        self.assertIn("已收金额：3000", query.stdout)
        self.assertNotIn("A2", query.stdout)

    def test_exact_invoice_amount_allowed(self) -> None:
        result = self.cli.allocate(
            code="A1", allocations=[("INV01", "4000")]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("已收齐", self.cli.query().stdout)

    def test_no_allocation_items_rejected(self) -> None:
        # argparse：缺少必需的 --allocation，非零退出且标准输出为空。
        result = self.cli.invoke(
            "receive-allocation",
            "--contract", "C001", "--code", "E1", "--date", "2026-05-10",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("--allocation", result.stderr)

    def test_invalid_date_rejected(self) -> None:
        result = self.cli.allocate(
            code="E1", date="2026/05/10", allocations=[("INV01", "100")]
        )
        self._assert_rejected(result)
        self.assertIn("收款日期", result.stderr)


if __name__ == "__main__":
    unittest.main()
