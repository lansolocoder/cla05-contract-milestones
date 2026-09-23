# 合同与付款里程碑台账

用于本地合同、付款里程碑与收付款记录管理的命令行项目。

需要 Python 3.12，无第三方依赖。在仓库根目录运行：

```bash
python3 -m contract_ledger --help
python3 -m contract_ledger --version
python3 -m unittest discover -s tests -v
```

当前仅提供帮助与版本查询入口；无参数显示帮助，未知参数以非零状态退出。尚未实现合同登记、里程碑状态流转、开票与收款登记以及变更单，不会创建业务数据文件。
