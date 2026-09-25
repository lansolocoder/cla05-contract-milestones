# 合同与付款里程碑台账

用于本地合同、付款里程碑与收付款记录管理的命令行项目。

需要 Python 3.12，无第三方依赖（仅标准库 `sqlite3`）。在仓库根目录运行：

```bash
python3 -m contract_ledger --help
python3 -m contract_ledger --version
python3 -m unittest discover -s tests -v
```

## 数据存储

所有业务数据持久化在项目内固定位置的本地 SQLite 数据库：
`.contract_ledger/ledger.db`（首次写操作时自动创建）。退出进程后重新
运行仍能读到既有记录。每次写操作在单个事务内完成，任何校验失败都会
整体回滚，不会留下半写入记录。

测试可用环境变量 `CONTRACT_LEDGER_DB` 指向临时数据库以隔离数据。

## 子命令

### 登记合同

```bash
python3 -m contract_ledger register-contract \
    --code HT-001 --customer 甲方公司 --date 2026-01-15 --amount 10000.50
```

- 合同编号全局唯一，重复登记以非零状态退出且不改动既有数据。
- 初始金额必须为正数，最多两位小数；签订日期必须为 `YYYY-MM-DD`。
- 成功后输出合同编号与当前金额（此时等于初始金额）。

### 登记变更单

```bash
python3 -m contract_ledger register-change \
    --contract HT-001 --code CHG-01 --description 追加需求 --delta 2000
```

- 变更单编号全局唯一；说明非空；变动可正可负、最多两位小数、不得为零。
- 登记后为**草稿**状态，不影响合同当前金额。

### 变更生效

```bash
python3 -m contract_ledger effect-change --code CHG-01
```

- 草稿 → 已生效，变动计入当前金额：
  当前金额 = 初始金额 + 全部已生效且未作废变更单的变动之和。
- 若生效后当前金额小于等于零则拒绝，金额与状态保持原样。

### 变更作废

```bash
python3 -m contract_ledger void-change --code CHG-01
```

- 已生效变更单作废后，其变动从当前金额中移除；若作废后当前金额
  小于等于零则拒绝。
- 草稿也可直接作废，金额不变；已作废记录保留可查，不得再生效。

### 查询合同

```bash
python3 -m contract_ledger query-contract --code HT-001
```

输出客户名称、签订日期、初始金额、当前金额，全部变更单（按登记先后
排列）以及全部里程碑的编号、标题、当前金额与状态，末尾输出
**已确认金额**。查询不改动任何数据；编号不存在则以非零状态退出并
说明原因。

- 无里程碑时输出 `里程碑：无`，已确认金额为 `0`。
- 已确认金额 = 全部**已确认**（未作废）里程碑当前金额之和。

### 登记付款里程碑

```bash
python3 -m contract_ledger register-milestone \
    --contract HT-001 --code MS-01 --title 首付款 \
    --amount 3000 --due 2026-03-01
```

- 里程碑编号全局唯一；标题非空；金额为正数、最多两位小数；
  到期日为 `YYYY-MM-DD`。引用的合同必须存在。
- 一个合同可登记多个里程碑，按登记先后排列；登记后为**待确认**。
- 里程碑**当前金额**随所属合同变更单生效或作废联动缩放：

  ```
  当前金额 = 登记金额 × 合同当前金额 ÷ 合同初始金额   （向下取整到分）
  ```

  合同当前金额不变（如变更单仍为草稿）时里程碑金额不变。

### 里程碑确认与作废

```bash
python3 -m contract_ledger confirm-milestone --code MS-01
python3 -m contract_ledger cancel-milestone  --code MS-01
```

- 待确认可确认为**已确认**，或作废为**已作废**。
- 已确认可作废；已作废为终态，不得再确认或重复作废。
- 只有已确认里程碑计入合同的"已确认金额"。

### 查询里程碑

```bash
python3 -m contract_ledger query-milestone --code MS-01
```

输出标题、所属合同编号、登记金额、当前金额、到期日与状态。里程碑
编号不存在时以非零状态退出，原因输出到标准错误。

## 错误处理

金额格式非法、日期不合法、编号缺失或重复、引用不存在的合同编号、
状态不允许的流转等，一律在单个事务内整体失败：非零状态退出，原因
输出到标准错误，不改动任何既有金额或状态。

开票与收款尚未实现，不在当前范围。
