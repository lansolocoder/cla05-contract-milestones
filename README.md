# 合同与付款里程碑台账

用于本地合同、付款里程碑与收付款记录管理的命令行项目。

需要 Python 3.12，无第三方依赖（仅使用标准库 sqlite3）。在仓库根目录运行：

```bash
python3 -m contract_ledger --help
python3 -m contract_ledger --version
python3 -m unittest discover -s tests -v
```

数据保存在项目根目录的 `contract_ledger.db`（SQLite），退出进程后重新运行仍可读到既有记录。

## 子命令

```bash
# 登记合同（编号全局唯一；初始金额为正数，最多两位小数）
python3 -m contract_ledger register-contract \
    --contract-id C-001 --customer 客户甲 --date 2026-09-01 --amount 10000.00

# 登记变更单（登记后为草稿，不计入当前金额；编号全局唯一）
python3 -m contract_ledger register-change \
    --contract-id C-001 --change-id CH-1 --description 追加需求 --delta 500.00

# 变更生效（计入当前金额；若生效后当前金额不大于零则拒绝）
python3 -m contract_ledger apply-change --change-id CH-1

# 变更作废（草稿或已生效均可作废；作废后不得再生效）
python3 -m contract_ledger void-change --change-id CH-1

# 查询合同（客户、签订日期、初始金额、当前金额及全部变更单）
python3 -m contract_ledger show-contract --contract-id C-001
```

合同当前金额 = 初始金额 + 全部已生效且未作废变更单的变动之和。所有写操作先整体校验再写入：金额或日期格式非法、编号缺失或重复、引用不存在的合同、状态不允许的流转等均以非零状态退出并在标准错误说明原因，不留下半写入记录。

里程碑、开票与收款尚未实现。
