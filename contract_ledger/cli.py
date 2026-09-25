"""Command-line entry point.

合同与变更单台账：所有业务数据保存在项目内固定位置的本地
SQLite 数据库文件（``.contract_ledger/ledger.db``），仅使用
Python 3.12 标准库。每次写操作在单个事务内完成，任何校验失败
都会整体回滚，不会留下半写入记录。
"""

import argparse
from collections.abc import Sequence
from datetime import date
from pathlib import Path
import os
import re
import sqlite3
import sys

from . import __version__

#: 数据库文件默认固定位于项目根目录（本文件向上两级）下；
#: CONTRACT_LEDGER_DB 环境变量可覆盖其位置（主要用于测试隔离）。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_DIR = PROJECT_ROOT / ".contract_ledger"
DB_PATH = DB_DIR / "ledger.db"

#: 定点金额文本：可选负号 + 至少一位整数 + 可选一到两位小数。
_AMOUNT_RE = re.compile(r"^-?\d+(?:\.\d{1,2})?$")

#: 变更单状态。
DRAFT = "draft"
EFFECTIVE = "effective"
VOID = "void"

#: 里程碑状态。
M_PENDING = "pending"
M_CONFIRMED = "confirmed"
M_VOIDED = "voided"

STATUS_LABELS = {
    DRAFT: "草稿",
    EFFECTIVE: "已生效",
    VOID: "已作废",
    M_PENDING: "待确认",
    M_CONFIRMED: "已确认",
    M_VOIDED: "已作废",
}


class LedgerError(Exception):
    """业务校验失败；信息输出到标准错误并以非零状态退出。"""


def resolve_db_path() -> Path:
    override = os.environ.get("CONTRACT_LEDGER_DB")
    return Path(override) if override else DB_PATH


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS contracts (
            id            INTEGER PRIMARY KEY,
            code          TEXT NOT NULL UNIQUE,
            customer      TEXT NOT NULL,
            signed_date   TEXT NOT NULL,
            initial_cents INTEGER NOT NULL CHECK (initial_cents > 0)
        );

        CREATE TABLE IF NOT EXISTS changes (
            id           INTEGER PRIMARY KEY,
            code         TEXT NOT NULL UNIQUE,
            contract_id  INTEGER NOT NULL
                                 REFERENCES contracts(id),
            description  TEXT NOT NULL,
            delta_cents  INTEGER NOT NULL CHECK (delta_cents <> 0),
            status       TEXT NOT NULL
                                 CHECK (status IN ('draft', 'effective', 'void')),
            seq          INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS milestones (
            id              INTEGER PRIMARY KEY,
            code            TEXT NOT NULL UNIQUE,
            contract_id     INTEGER NOT NULL
                                    REFERENCES contracts(id),
            title           TEXT NOT NULL,
            registered_cents INTEGER NOT NULL CHECK (registered_cents > 0),
            due_date        TEXT NOT NULL,
            status          TEXT NOT NULL
                                    CHECK (status IN ('pending', 'confirmed', 'voided')),
            seq             INTEGER NOT NULL
        );
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 输入解析
# ---------------------------------------------------------------------------

def parse_amount(text: str, *, field: str, allow_negative: bool) -> int:
    """把金额文本解析为整数分。

    只接受定点写法（可选负号、整数部分至少一位、最多两位小数），
    拒绝科学计数法、空串与多余空白。初始金额必须为正数，变动金额
    不得为零。
    """
    if not isinstance(text, str) or not _AMOUNT_RE.match(text):
        raise LedgerError(f"{field}格式非法：{text!r}（应为数字，最多两位小数）")
    neg = text.startswith("-")
    body = text[1:] if neg else text
    if "." in body:
        whole, frac = body.split(".", 1)
        frac = (frac + "00")[:2]
    else:
        whole, frac = body, "00"
    cents = int(whole) * 100 + int(frac)
    if neg:
        cents = -cents
    if not allow_negative:
        if cents <= 0:
            raise LedgerError(f"{field}必须为正数：{text!r}")
    elif cents == 0:
        raise LedgerError(f"{field}不得为零：{text!r}")
    return cents


def parse_iso_date(text: str, *, field: str) -> str:
    """校验 YYYY-MM-DD 日期并返回规范化文本。"""
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise LedgerError(f"{field}不合法：{text!r}（应为 YYYY-MM-DD）")
    if parsed.isoformat() != text:
        raise LedgerError(f"{field}不合法：{text!r}（应为 YYYY-MM-DD）")
    return parsed.isoformat()


def parse_signed_date(text: str) -> str:
    """校验签订日期。"""
    return parse_iso_date(text, field="签订日期")


def require_text(value: str | None, field: str) -> str:
    if value is None or not value.strip():
        raise LedgerError(f"{field}缺失或为空")
    return value


def format_yuan(cents: int) -> str:
    """把整数分格式化为元（最多两位小数，无多余尾零）。"""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    yuan, rem = divmod(cents, 100)
    if rem == 0:
        return f"{sign}{yuan}"
    return f"{sign}{yuan}.{rem:02d}"


# ---------------------------------------------------------------------------
# 业务操作（均在调用方提供的事务内执行）
# ---------------------------------------------------------------------------

def get_contract(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM contracts WHERE code = ?", (code,)
    ).fetchone()


def get_change(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM changes WHERE code = ?", (code,)
    ).fetchone()


def current_cents(conn: sqlite3.Connection, contract_id: int) -> int:
    row = conn.execute(
        """
        SELECT c.initial_cents + COALESCE(SUM(ch.delta_cents), 0)
          FROM contracts AS c
          LEFT JOIN changes AS ch
            ON ch.contract_id = c.id
           AND ch.status = 'effective'
         WHERE c.id = ?
         GROUP BY c.id
        """,
        (contract_id,),
    ).fetchone()
    return int(row[0])


def register_contract(
    conn: sqlite3.Connection,
    code: str,
    customer: str,
    signed_date_text: str,
    initial_text: str,
) -> tuple[str, int]:
    code = require_text(code, "合同编号")
    customer = require_text(customer, "客户名称")
    signed_date_text = parse_signed_date(signed_date_text)
    initial_cents = parse_amount(
        initial_text, field="初始金额", allow_negative=False
    )
    if get_contract(conn, code) is not None:
        raise LedgerError(f"合同编号已存在，不得重复登记：{code}")
    conn.execute(
        """
        INSERT INTO contracts (code, customer, signed_date, initial_cents)
        VALUES (?, ?, ?, ?)
        """,
        (code, customer, signed_date_text, initial_cents),
    )
    return code, initial_cents


def register_change(
    conn: sqlite3.Connection,
    contract_code: str,
    change_code: str,
    description: str,
    delta_text: str,
) -> str:
    contract_code = require_text(contract_code, "合同编号")
    change_code = require_text(change_code, "变更单编号")
    description = require_text(description, "变更说明")
    delta_cents = parse_amount(
        delta_text, field="金额变动", allow_negative=True
    )
    contract = get_contract(conn, contract_code)
    if contract is None:
        raise LedgerError(f"引用的合同编号不存在：{contract_code}")
    if get_change(conn, change_code) is not None:
        raise LedgerError(f"变更单编号已存在，不得重复登记：{change_code}")
    next_seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM changes WHERE contract_id = ?",
        (contract["id"],),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO changes
            (code, contract_id, description, delta_cents, status, seq)
        VALUES (?, ?, ?, ?, 'draft', ?)
        """,
        (change_code, contract["id"], description, delta_cents, next_seq),
    )
    return change_code


def _transition(
    conn: sqlite3.Connection, change_code: str, *, action: str
) -> sqlite3.Row:
    change_code = require_text(change_code, "变更单编号")
    change = get_change(conn, change_code)
    if change is None:
        raise LedgerError(f"变更单编号不存在：{change_code}")

    if action == "effect":
        if change["status"] == EFFECTIVE:
            raise LedgerError(f"变更单已生效，不能重复生效：{change_code}")
        if change["status"] == VOID:
            raise LedgerError(f"变更单已作废，不得再生效：{change_code}")
        new_status = EFFECTIVE
    else:  # void
        if change["status"] == VOID:
            raise LedgerError(f"变更单已作废，不能重复作废：{change_code}")
        new_status = VOID

    # 草稿作废不影响金额，无需校验余额。
    if change["status"] == DRAFT and action == "void":
        projected = None
    else:
        projected = current_cents(conn, change["contract_id"])
        if action == "effect":
            projected += change["delta_cents"]
        else:  # 已生效 -> 作废，移除其变动
            projected -= change["delta_cents"]
        if projected <= 0:
            verb = "生效" if action == "effect" else "作废"
            raise LedgerError(
                f"{verb}后合同当前金额将小于等于零，拒绝{verb}：{change_code}"
            )

    conn.execute(
        "UPDATE changes SET status = ? WHERE id = ?",
        (new_status, change["id"]),
    )
    return conn.execute("SELECT * FROM changes WHERE id = ?", (change["id"],)).fetchone()


def effect_change(conn: sqlite3.Connection, change_code: str) -> sqlite3.Row:
    return _transition(conn, change_code, action="effect")


def void_change(conn: sqlite3.Connection, change_code: str) -> sqlite3.Row:
    return _transition(conn, change_code, action="void")


def query_contract(conn: sqlite3.Connection, code: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    code = require_text(code, "合同编号")
    contract = get_contract(conn, code)
    if contract is None:
        raise LedgerError(f"合同编号不存在：{code}")
    changes = conn.execute(
        "SELECT * FROM changes WHERE contract_id = ? ORDER BY seq",
        (contract["id"],),
    ).fetchall()
    return contract, changes


# ---------------------------------------------------------------------------
# 里程碑操作
# ---------------------------------------------------------------------------

def get_milestone(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM milestones WHERE code = ?", (code,)
    ).fetchone()


def milestone_current_cents(
    registered_cents: int, initial_cents: int, current_contract_cents: int
) -> int:
    """里程碑当前金额 = 登记金额 × 合同当前金额 ÷ 初始金额，向下取整到分。"""
    return (registered_cents * current_contract_cents) // initial_cents


def list_milestones(conn: sqlite3.Connection, contract_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM milestones WHERE contract_id = ? ORDER BY seq",
        (contract_id,),
    ).fetchall()


def confirmed_cents(conn: sqlite3.Connection, contract_id: int) -> int:
    """已确认金额：当前处于已确认状态（已确认且未作废）的里程碑当前金额之和。"""
    contract = conn.execute(
        "SELECT initial_cents FROM contracts WHERE id = ?", (contract_id,)
    ).fetchone()
    total_cents = current_cents(conn, contract_id)
    total = 0
    rows = conn.execute(
        "SELECT registered_cents FROM milestones "
        "WHERE contract_id = ? AND status = 'confirmed' ORDER BY seq",
        (contract_id,),
    ).fetchall()
    for row in rows:
        total += milestone_current_cents(
            row["registered_cents"], contract["initial_cents"], total_cents
        )
    return total


def register_milestone(
    conn: sqlite3.Connection,
    contract_code: str,
    milestone_code: str,
    title: str,
    amount_text: str,
    due_text: str,
) -> str:
    contract_code = require_text(contract_code, "合同编号")
    milestone_code = require_text(milestone_code, "里程碑编号")
    title = require_text(title, "里程碑标题")
    amount_cents = parse_amount(
        amount_text, field="里程碑金额", allow_negative=False
    )
    due_date = parse_iso_date(due_text, field="到期日")
    contract = get_contract(conn, contract_code)
    if contract is None:
        raise LedgerError(f"引用的合同编号不存在：{contract_code}")
    if get_milestone(conn, milestone_code) is not None:
        raise LedgerError(f"里程碑编号已存在，不得重复登记：{milestone_code}")
    next_seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM milestones WHERE contract_id = ?",
        (contract["id"],),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO milestones
            (code, contract_id, title, registered_cents, due_date, status, seq)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (milestone_code, contract["id"], title, amount_cents, due_date, next_seq),
    )
    return milestone_code


def _milestone_transition(
    conn: sqlite3.Connection, milestone_code: str, *, action: str
) -> sqlite3.Row:
    milestone_code = require_text(milestone_code, "里程碑编号")
    milestone = get_milestone(conn, milestone_code)
    if milestone is None:
        raise LedgerError(f"里程碑编号不存在：{milestone_code}")

    if action == "confirm":
        if milestone["status"] == M_CONFIRMED:
            raise LedgerError(f"里程碑已确认，不能重复确认：{milestone_code}")
        if milestone["status"] == M_VOIDED:
            raise LedgerError(f"里程碑已作废，不得再确认：{milestone_code}")
        new_status = M_CONFIRMED
    else:  # cancel
        if milestone["status"] == M_VOIDED:
            raise LedgerError(f"里程碑已作废，不能重复作废：{milestone_code}")
        new_status = M_VOIDED

    conn.execute(
        "UPDATE milestones SET status = ? WHERE id = ?",
        (new_status, milestone["id"]),
    )
    return conn.execute(
        "SELECT * FROM milestones WHERE id = ?", (milestone["id"],)
    ).fetchone()


def confirm_milestone(conn: sqlite3.Connection, milestone_code: str) -> sqlite3.Row:
    return _milestone_transition(conn, milestone_code, action="confirm")


def cancel_milestone(conn: sqlite3.Connection, milestone_code: str) -> sqlite3.Row:
    return _milestone_transition(conn, milestone_code, action="cancel")


def query_milestone(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    code = require_text(code, "里程碑编号")
    milestone = get_milestone(conn, code)
    if milestone is None:
        raise LedgerError(f"里程碑编号不存在：{code}")
    return milestone


# ---------------------------------------------------------------------------
# 命令处理
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contract-ledger",
        description="本地合同与变更单台账（SQLite 持久化，无第三方依赖）。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="子命令")

    p = subparsers.add_parser(
        "register-contract",
        aliases=["add-contract"],
        help="登记合同",
        description="登记一份新合同。合同编号全局唯一，初始金额必须为正数。",
    )
    p.add_argument("--code", required=True, help="合同编号（全局唯一）")
    p.add_argument("--customer", required=True, help="客户名称")
    p.add_argument("--date", required=True, help="签订日期（YYYY-MM-DD）")
    p.add_argument("--amount", required=True, help="初始金额（元，正数，最多两位小数）")
    p.set_defaults(handler=cmd_register_contract)

    p = subparsers.add_parser(
        "register-change",
        aliases=["add-change"],
        help="登记变更单（草稿）",
        description="为已有合同登记一张草稿变更单，不影响合同当前金额。",
    )
    p.add_argument("--contract", required=True, help="所属合同编号")
    p.add_argument("--code", required=True, help="变更单编号（全局唯一）")
    p.add_argument("--description", required=True, help="变更说明（非空）")
    p.add_argument("--delta", required=True, help="金额变动（元，可正可负，不得为零）")
    p.set_defaults(handler=cmd_register_change)

    p = subparsers.add_parser(
        "effect-change",
        help="变更单生效",
        description="把草稿变更单置为已生效；生效后当前金额必须大于零。",
    )
    p.add_argument("--code", required=True, help="变更单编号")
    p.set_defaults(handler=cmd_effect_change)

    p = subparsers.add_parser(
        "void-change",
        help="变更单作废",
        description="作废草稿或已生效变更单；已生效变更单作废后当前金额必须大于零。",
    )
    p.add_argument("--code", required=True, help="变更单编号")
    p.set_defaults(handler=cmd_void_change)

    p = subparsers.add_parser(
        "query-contract",
        aliases=["show-contract"],
        help="查询合同及其全部变更单",
        description="按合同编号输出合同信息与全部变更单（按登记先后排列），不改动数据。",
    )
    p.add_argument("--code", required=True, help="合同编号")
    p.set_defaults(handler=cmd_query_contract)

    p = subparsers.add_parser(
        "register-milestone",
        aliases=["add-milestone"],
        help="登记付款里程碑（待确认）",
        description="为已有合同登记付款里程碑，登记后为待确认状态；编号全局唯一。",
    )
    p.add_argument("--contract", required=True, help="所属合同编号")
    p.add_argument("--code", required=True, help="里程碑编号（全局唯一）")
    p.add_argument("--title", required=True, help="里程碑标题（非空）")
    p.add_argument("--amount", required=True, help="登记金额（元，正数，最多两位小数）")
    p.add_argument("--due", required=True, help="到期日（YYYY-MM-DD）")
    p.set_defaults(handler=cmd_register_milestone)

    p = subparsers.add_parser(
        "confirm-milestone",
        help="确认里程碑",
        description="把待确认里程碑置为已确认；已作废里程碑不得再确认。",
    )
    p.add_argument("--code", required=True, help="里程碑编号")
    p.set_defaults(handler=cmd_confirm_milestone)

    p = subparsers.add_parser(
        "cancel-milestone",
        help="作废里程碑",
        description="作废待确认或已确认里程碑；已作废为终态，不得重复作废。",
    )
    p.add_argument("--code", required=True, help="里程碑编号")
    p.set_defaults(handler=cmd_cancel_milestone)

    p = subparsers.add_parser(
        "query-milestone",
        aliases=["show-milestone"],
        help="查询单个里程碑",
        description="按里程碑编号输出标题、所属合同、金额、到期日与状态，不改动数据。",
    )
    p.add_argument("--code", required=True, help="里程碑编号")
    p.set_defaults(handler=cmd_query_milestone)

    return parser


def cmd_register_contract(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    with conn:  # 单事务：失败自动回滚
        code, initial_cents = register_contract(
            conn, args.code, args.customer, args.date, args.amount
        )
    print(f"合同编号：{code}")
    print(f"当前金额：{format_yuan(initial_cents)}")
    return 0


def cmd_register_change(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    with conn:
        code = register_change(
            conn, args.contract, args.code, args.description, args.delta
        )
    print(f"变更单已登记（草稿）：{code}")
    return 0


def cmd_effect_change(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    with conn:
        change = effect_change(conn, args.code)
        amount = current_cents(conn, change["contract_id"])
    print(f"变更单已生效：{change['code']}")
    print(f"当前金额：{format_yuan(amount)}")
    return 0


def cmd_void_change(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    with conn:
        change = void_change(conn, args.code)
        amount = current_cents(conn, change["contract_id"])
    print(f"变更单已作废：{change['code']}")
    print(f"当前金额：{format_yuan(amount)}")
    return 0


def cmd_query_contract(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    contract, changes = query_contract(conn, args.code)
    amount = current_cents(conn, contract["id"])
    milestones = list_milestones(conn, contract["id"])
    print(f"合同编号：{contract['code']}")
    print(f"客户名称：{contract['customer']}")
    print(f"签订日期：{contract['signed_date']}")
    print(f"初始金额：{format_yuan(contract['initial_cents'])}")
    print(f"当前金额：{format_yuan(amount)}")
    if changes:
        print("变更单：")
        for ch in changes:
            print(
                f"  - {ch['code']}  变动：{format_yuan(ch['delta_cents'])}  "
                f"状态：{STATUS_LABELS[ch['status']]}"
            )
    else:
        print("变更单：无")
    if milestones:
        print("里程碑：")
        for ms in milestones:
            current = milestone_current_cents(
                ms["registered_cents"], contract["initial_cents"], amount
            )
            print(
                f"  - {ms['code']}  标题：{ms['title']}  "
                f"当前金额：{format_yuan(current)}  "
                f"状态：{STATUS_LABELS[ms['status']]}"
            )
    else:
        print("里程碑：无")
    print(f"已确认金额：{format_yuan(confirmed_cents(conn, contract['id']))}")
    return 0


def cmd_register_milestone(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    with conn:  # 单事务：失败自动回滚
        code = register_milestone(
            conn, args.contract, args.code, args.title, args.amount, args.due
        )
    print(f"里程碑已登记（待确认）：{code}")
    return 0


def cmd_confirm_milestone(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    with conn:
        milestone = confirm_milestone(conn, args.code)
    print(f"里程碑已确认：{milestone['code']}")
    return 0


def cmd_cancel_milestone(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    with conn:
        milestone = cancel_milestone(conn, args.code)
    print(f"里程碑已作废：{milestone['code']}")
    return 0


def cmd_query_milestone(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    milestone = query_milestone(conn, args.code)
    contract = conn.execute(
        "SELECT * FROM contracts WHERE id = ?", (milestone["contract_id"],)
    ).fetchone()
    amount = current_cents(conn, contract["id"])
    current = milestone_current_cents(
        milestone["registered_cents"], contract["initial_cents"], amount
    )
    print(f"里程碑编号：{milestone['code']}")
    print(f"标题：{milestone['title']}")
    print(f"所属合同编号：{contract['code']}")
    print(f"登记金额：{format_yuan(milestone['registered_cents'])}")
    print(f"当前金额：{format_yuan(current)}")
    print(f"到期日：{milestone['due_date']}")
    print(f"状态：{STATUS_LABELS[milestone['status']]}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0

    conn = connect()
    try:
        init_db(conn)
        try:
            return handler(args, conn)
        except LedgerError as exc:
            conn.rollback()
            print(f"错误：{exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()
