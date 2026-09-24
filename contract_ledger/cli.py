"""Command-line entry point."""

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from . import store


def _cmd_register_contract(args: argparse.Namespace) -> int:
    sign_date = store.parse_date(args.date)
    amount = store.parse_initial_amount(args.amount)
    with store.connect() as conn:
        store.register_contract(conn, args.contract_id, args.customer, sign_date, amount)
        current = store.get_contract(conn, args.contract_id).current_amount_cents
    print(f"合同 {args.contract_id} 登记成功，当前金额 {store.format_cents(current)} 元")
    return 0


def _cmd_register_change(args: argparse.Namespace) -> int:
    delta = store.parse_delta_amount(args.delta)
    with store.connect() as conn:
        store.register_change(
            conn, args.contract_id, args.change_id, args.description, delta
        )
    print(f"变更单 {args.change_id} 登记成功（草稿），暂未计入合同金额")
    return 0


def _cmd_apply_change(args: argparse.Namespace) -> int:
    with store.connect() as conn:
        contract_id, current = store.apply_change(conn, args.change_id)
    print(
        f"变更单 {args.change_id} 已生效，合同 {contract_id} "
        f"当前金额 {store.format_cents(current)} 元"
    )
    return 0


def _cmd_void_change(args: argparse.Namespace) -> int:
    with store.connect() as conn:
        contract_id, current = store.void_change(conn, args.change_id)
    print(
        f"变更单 {args.change_id} 已作废，合同 {contract_id} "
        f"当前金额 {store.format_cents(current)} 元"
    )
    return 0


def _cmd_show_contract(args: argparse.Namespace) -> int:
    with store.connect() as conn:
        contract = store.get_contract(conn, args.contract_id)
    print(f"合同编号: {contract.contract_id}")
    print(f"客户名称: {contract.customer}")
    print(f"签订日期: {contract.sign_date}")
    print(f"初始金额: {store.format_cents(contract.initial_amount_cents)} 元")
    print(f"当前金额: {store.format_cents(contract.current_amount_cents)} 元")
    if contract.changes:
        print("变更单:")
        for change in contract.changes:
            label = store.STATUS_LABELS[change.status]
            delta = store.format_signed_cents(change.delta_cents)
            print(f"  {change.change_id}  {delta} 元  {label}  {change.description}")
    else:
        print("变更单: 无")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contract-ledger",
        description="本地合同与变更单台账（数据保存在项目内 contract_ledger.db）。",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(title="子命令", metavar="<子命令>")

    p = subparsers.add_parser(
        "register-contract",
        help="登记合同",
        description="登记合同：编号全局唯一，初始金额为正数（元，最多两位小数）。",
    )
    p.add_argument("--contract-id", required=True, help="合同编号（全局唯一）")
    p.add_argument("--customer", required=True, help="客户名称")
    p.add_argument("--date", required=True, help="签订日期，格式 YYYY-MM-DD")
    p.add_argument("--amount", required=True, help="初始金额（元，正数，最多两位小数）")
    p.set_defaults(handler=_cmd_register_contract)

    p = subparsers.add_parser(
        "register-change",
        help="登记变更单（草稿）",
        description="登记变更单：登记后为草稿状态，不影响合同当前金额。",
    )
    p.add_argument("--contract-id", required=True, help="所属合同编号")
    p.add_argument("--change-id", required=True, help="变更单编号（全局唯一）")
    p.add_argument("--description", required=True, help="变更说明（非空）")
    p.add_argument(
        "--delta", required=True, help="金额变动（元，可正可负，最多两位小数，非零）"
    )
    p.set_defaults(handler=_cmd_register_change)

    p = subparsers.add_parser(
        "apply-change",
        help="变更生效",
        description="变更生效：草稿变更单生效后计入合同当前金额。",
    )
    p.add_argument("--change-id", required=True, help="变更单编号")
    p.set_defaults(handler=_cmd_apply_change)

    p = subparsers.add_parser(
        "void-change",
        help="变更作废",
        description="变更作废：草稿或已生效变更单作废；作废后不得再生效。",
    )
    p.add_argument("--change-id", required=True, help="变更单编号")
    p.set_defaults(handler=_cmd_void_change)

    p = subparsers.add_parser(
        "show-contract",
        help="查询合同",
        description="查询合同：输出合同信息与全部变更单（按登记先后排列）。",
    )
    p.add_argument("--contract-id", required=True, help="合同编号")
    p.set_defaults(handler=_cmd_show_contract)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    try:
        return handler(args)
    except store.LedgerError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
