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

STATUS_LABELS = {
    DRAFT: "草稿",
    EFFECTIVE: "已生效",
    VOID: "已作废",
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


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


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

        CREATE TABLE IF NOT EXISTS milestones (
            id            INTEGER PRIMARY KEY,
            code          TEXT NOT NULL UNIQUE,
            contract_id   INTEGER NOT NULL
                                 REFERENCES contracts(id),
            name          TEXT NOT NULL,
            initial_cents INTEGER NOT NULL CHECK (initial_cents > 0),
            due_date      TEXT NOT NULL,
            seq           INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS changes (
            id            INTEGER PRIMARY KEY,
            code          TEXT NOT NULL UNIQUE,
            contract_id   INTEGER NOT NULL
                                  REFERENCES contracts(id),
            milestone_id  INTEGER
                                  REFERENCES milestones(id),
            description   TEXT NOT NULL,
            delta_cents   INTEGER NOT NULL CHECK (delta_cents <> 0),
            status        TEXT NOT NULL
                                  CHECK (status IN ('draft', 'effective', 'void')),
            effective_date TEXT,
            seq           INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS payments (
            id            INTEGER PRIMARY KEY,
            code          TEXT NOT NULL UNIQUE,
            contract_id   INTEGER NOT NULL
                                  REFERENCES contracts(id),
            milestone_id  INTEGER
                                  REFERENCES milestones(id),
            amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
            paid_date     TEXT NOT NULL,
            seq           INTEGER NOT NULL
        );
        """
    )
    # 旧版本数据库可能缺少里程碑相关列，原地补齐（均不影响既有数据）。
    change_columns = _column_names(conn, "changes")
    if "milestone_id" not in change_columns:
        conn.execute("ALTER TABLE changes ADD COLUMN milestone_id INTEGER")
    if "effective_date" not in change_columns:
        conn.execute("ALTER TABLE changes ADD COLUMN effective_date TEXT")
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
    except (TypeError, ValueError):
        raise LedgerError(f"{field}不合法：{text!r}（应为 YYYY-MM-DD）")
    if parsed.isoformat() != text:
        raise LedgerError(f"{field}不合法：{text!r}（应为 YYYY-MM-DD）")
    return parsed.isoformat()


def parse_signed_date(text: str) -> str:
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


def get_contract_by_id(conn: sqlite3.Connection, contract_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM contracts WHERE id = ?", (contract_id,)
    ).fetchone()


def get_change(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM changes WHERE code = ?", (code,)
    ).fetchone()


def get_milestone(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM milestones WHERE code = ?", (code,)
    ).fetchone()


def list_milestones(
    conn: sqlite3.Connection, contract_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM milestones WHERE contract_id = ? ORDER BY seq",
        (contract_id,),
    ).fetchall()


def get_payment(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM payments WHERE code = ?", (code,)
    ).fetchone()


def list_payments(
    conn: sqlite3.Connection, contract_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM payments WHERE contract_id = ? ORDER BY seq",
        (contract_id,),
    ).fetchall()


def contract_payment_sum(conn: sqlite3.Connection, contract_id: int) -> int:
    """该合同全部收款单（合同级 + 归属里程碑）金额合计。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE contract_id = ?",
        (contract_id,),
    ).fetchone()
    return int(row[0])


def milestone_payment_sum(conn: sqlite3.Connection, milestone_id: int) -> int:
    """该里程碑全部收款单金额合计。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE milestone_id = ?",
        (milestone_id,),
    ).fetchone()
    return int(row[0])


def milestone_change_sum(conn: sqlite3.Connection, milestone_id: int) -> int:
    """该里程碑下已生效且未作废变更单的变动合计。"""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(delta_cents), 0)
          FROM changes
         WHERE milestone_id = ?
           AND status = 'effective'
        """,
        (milestone_id,),
    ).fetchone()
    return int(row[0])


def milestone_current_cents(conn: sqlite3.Connection, milestone_id: int) -> int:
    milestone = conn.execute(
        "SELECT initial_cents FROM milestones WHERE id = ?", (milestone_id,)
    ).fetchone()
    return int(milestone["initial_cents"]) + milestone_change_sum(conn, milestone_id)


def latest_effective_change_date(
    conn: sqlite3.Connection, milestone_id: int
) -> str | None:
    """该里程碑下已生效且未作废变更单中最晚的变更生效日期。"""
    row = conn.execute(
        """
        SELECT MAX(effective_date)
          FROM changes
         WHERE milestone_id = ?
           AND status = 'effective'
        """,
        (milestone_id,),
    ).fetchone()
    return row[0]


def register_milestone(
    conn: sqlite3.Connection,
    contract_code: str,
    milestone_code: str,
    name: str,
    amount_text: str,
    due_text: str,
) -> str:
    contract_code = require_text(contract_code, "合同编号")
    milestone_code = require_text(milestone_code, "里程碑编号")
    name = require_text(name, "里程碑名称")
    amount_cents = parse_amount(
        amount_text, field="里程碑金额", allow_negative=False
    )
    due_date = parse_iso_date(due_text, field="到期日")
    contract = get_contract(conn, contract_code)
    if contract is None:
        raise LedgerError(f"引用的合同编号不存在：{contract_code}")
    if get_milestone(conn, milestone_code) is not None:
        raise LedgerError(f"里程碑编号已存在，不得重复登记：{milestone_code}")
    if due_date < contract["signed_date"]:
        raise LedgerError(
            f"到期日不得早于合同签订日期：{due_date} 早于 {contract['signed_date']}"
        )
    next_seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM milestones WHERE contract_id = ?",
        (contract["id"],),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO milestones
            (code, contract_id, name, initial_cents, due_date, seq)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (milestone_code, contract["id"], name, amount_cents, due_date, next_seq),
    )
    return milestone_code


def set_milestone_due(
    conn: sqlite3.Connection, milestone_code: str, due_text: str
) -> tuple[str, str]:
    milestone_code = require_text(milestone_code, "里程碑编号")
    due_date = parse_iso_date(due_text, field="到期日")
    milestone = get_milestone(conn, milestone_code)
    if milestone is None:
        raise LedgerError(f"里程碑编号不存在：{milestone_code}")
    contract = get_contract_by_id(conn, milestone["contract_id"])
    if due_date < contract["signed_date"]:
        raise LedgerError(
            f"新到期日不得早于合同签订日期：{due_date} 早于 {contract['signed_date']}"
        )
    latest_effective = latest_effective_change_date(conn, milestone["id"])
    if latest_effective is not None and due_date < latest_effective:
        raise LedgerError(
            f"新到期日不得早于已生效变更单的最晚生效日期："
            f"{due_date} 早于 {latest_effective}"
        )
    conn.execute(
        "UPDATE milestones SET due_date = ? WHERE id = ?",
        (due_date, milestone["id"]),
    )
    return milestone_code, due_date


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
    milestone_code: str | None = None,
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
    milestone_id = None
    if milestone_code is not None:
        milestone_code = require_text(milestone_code, "里程碑编号")
        milestone = get_milestone(conn, milestone_code)
        if milestone is None:
            raise LedgerError(f"里程碑编号不存在：{milestone_code}")
        if milestone["contract_id"] != contract["id"]:
            raise LedgerError(
                f"里程碑不属于该合同：里程碑 {milestone_code} "
                f"不属于合同 {contract_code}"
            )
        milestone_id = milestone["id"]
    if get_change(conn, change_code) is not None:
        raise LedgerError(f"变更单编号已存在，不得重复登记：{change_code}")
    next_seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM changes WHERE contract_id = ?",
        (contract["id"],),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO changes
            (code, contract_id, milestone_id, description, delta_cents, status, seq)
        VALUES (?, ?, ?, ?, ?, 'draft', ?)
        """,
        (change_code, contract["id"], milestone_id, description,
         delta_cents, next_seq),
    )
    return change_code


def register_payment(
    conn: sqlite3.Connection,
    contract_code: str,
    payment_code: str,
    amount_text: str,
    paid_date_text: str,
    milestone_code: str | None = None,
) -> tuple[str, int]:
    """登记收款单（登记即生效），返回编号与对应口径下的登记后收款合计（分）。"""
    contract_code = require_text(contract_code, "合同编号")
    payment_code = require_text(payment_code, "收款单编号")
    amount_cents = parse_amount(
        amount_text, field="收款金额", allow_negative=False
    )
    paid_date = parse_iso_date(paid_date_text, field="收款日期")
    contract = get_contract(conn, contract_code)
    if contract is None:
        raise LedgerError(f"引用的合同编号不存在：{contract_code}")
    milestone_id = None
    if milestone_code is not None:
        milestone_code = require_text(milestone_code, "里程碑编号")
        milestone = get_milestone(conn, milestone_code)
        if milestone is None:
            raise LedgerError(f"里程碑编号不存在：{milestone_code}")
        if milestone["contract_id"] != contract["id"]:
            raise LedgerError(
                f"里程碑不属于该合同：里程碑 {milestone_code} "
                f"不属于合同 {contract_code}"
            )
        milestone_id = milestone["id"]
    if get_payment(conn, payment_code) is not None:
        raise LedgerError(f"收款单编号已存在，不得重复登记：{payment_code}")
    if paid_date < contract["signed_date"]:
        raise LedgerError(
            f"收款日期不得早于合同签订日期：{paid_date} 早于 "
            f"{contract['signed_date']}"
        )
    # 合同级未超收：全部收款单（合同级 + 归属里程碑）合计不得超过合同当前金额。
    contract_limit = current_cents(conn, contract["id"])
    contract_total = contract_payment_sum(conn, contract["id"]) + amount_cents
    if contract_total > contract_limit:
        raise LedgerError(
            f"登记后合同收款合计 {format_yuan(contract_total)} 将超过合同当前金额 "
            f"{format_yuan(contract_limit)}，拒绝登记：{payment_code}"
        )
    # 里程碑级未超收：该里程碑收款合计不得超过其当前金额。
    if milestone_id is not None:
        milestone_limit = milestone_current_cents(conn, milestone_id)
        milestone_total = (
            milestone_payment_sum(conn, milestone_id) + amount_cents
        )
        if milestone_total > milestone_limit:
            raise LedgerError(
                f"登记后里程碑收款合计 {format_yuan(milestone_total)} 将超过"
                f"里程碑当前金额 {format_yuan(milestone_limit)}，"
                f"拒绝登记：{payment_code}"
            )
        total_cents = milestone_total
    else:
        total_cents = contract_total
    next_seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM payments WHERE contract_id = ?",
        (contract["id"],),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO payments
            (code, contract_id, milestone_id, amount_cents, paid_date, seq)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (payment_code, contract["id"], milestone_id, amount_cents,
         paid_date, next_seq),
    )
    return payment_code, total_cents


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

    # 里程碑变更单生效还要保证该里程碑当前金额大于零；作废沿用
    # 既有合同级规则，草稿作废一律不校验。
    if action == "effect" and change["milestone_id"] is not None:
        milestone_projected = milestone_current_cents(
            conn, change["milestone_id"]
        ) + change["delta_cents"]
        if milestone_projected <= 0:
            raise LedgerError(
                f"生效后里程碑当前金额将小于等于零，拒绝生效：{change_code}"
            )

    if action == "effect":
        conn.execute(
            "UPDATE changes SET status = ?, effective_date = ? WHERE id = ?",
            (new_status, date.today().isoformat(), change["id"]),
        )
    else:
        conn.execute(
            "UPDATE changes SET status = ? WHERE id = ?",
            (new_status, change["id"]),
        )
    return conn.execute("SELECT * FROM changes WHERE id = ?", (change["id"],)).fetchone()


def effect_change(conn: sqlite3.Connection, change_code: str) -> sqlite3.Row:
    return _transition(conn, change_code, action="effect")


def void_change(conn: sqlite3.Connection, change_code: str) -> sqlite3.Row:
    return _transition(conn, change_code, action="void")


def query_contract(
    conn: sqlite3.Connection, code: str
) -> tuple[sqlite3.Row, list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row]]:
    code = require_text(code, "合同编号")
    contract = get_contract(conn, code)
    if contract is None:
        raise LedgerError(f"合同编号不存在：{code}")
    changes = conn.execute(
        "SELECT * FROM changes WHERE contract_id = ? ORDER BY seq",
        (contract["id"],),
    ).fetchall()
    milestones = list_milestones(conn, contract["id"])
    payments = list_payments(conn, contract["id"])
    return contract, changes, milestones, payments


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
        description=(
            "为已有合同登记一张草稿变更单，不影响合同当前金额。"
            "可用 --milestone 归属到该合同下的里程碑，省略则归属合同总额。"
        ),
    )
    p.add_argument("--contract", required=True, help="所属合同编号")
    p.add_argument("--code", required=True, help="变更单编号（全局唯一）")
    p.add_argument("--description", required=True, help="变更说明（非空）")
    p.add_argument("--delta", required=True, help="金额变动（元，可正可负，不得为零）")
    p.add_argument(
        "--milestone",
        help="归属里程碑编号（省略则归属合同总额；指定时必须属于该合同）",
    )
    p.set_defaults(handler=cmd_register_change)

    p = subparsers.add_parser(
        "register-milestone",
        aliases=["add-milestone"],
        help="登记付款里程碑",
        description=(
            "为已有合同登记付款里程碑。里程碑编号全局唯一，金额为正数，"
            "到期日不得早于合同签订日期。"
        ),
    )
    p.add_argument("--contract", required=True, help="所属合同编号")
    p.add_argument("--code", required=True, help="里程碑编号（全局唯一）")
    p.add_argument("--name", required=True, help="里程碑名称（非空）")
    p.add_argument("--amount", required=True, help="里程碑金额（元，正数，最多两位小数）")
    p.add_argument("--due", required=True, help="到期日（YYYY-MM-DD，不得早于合同签订日期）")
    p.set_defaults(handler=cmd_register_milestone)

    p = subparsers.add_parser(
        "set-milestone-due",
        help="更新里程碑到期日",
        description=(
            "更新里程碑到期日；新到期日不得早于合同签订日期，也不得早于"
            "该里程碑下已生效变更单的最晚生效日期。"
        ),
    )
    p.add_argument("--code", required=True, help="里程碑编号")
    p.add_argument("--due", required=True, help="新到期日（YYYY-MM-DD）")
    p.set_defaults(handler=cmd_set_milestone_due)

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
        "record-payment",
        aliases=["add-payment"],
        help="登记收款单",
        description=(
            "为已有合同登记一张收款单，登记即生效。收款单编号全局唯一，"
            "金额为正数，收款日期不得早于合同签订日期；可用 --milestone "
            "归属到该合同下的里程碑，省略则计入合同级收款合计。"
            "登记后对应口径下的收款合计不得超过合同/里程碑当前金额。"
        ),
    )
    p.add_argument("--contract", required=True, help="所属合同编号")
    p.add_argument("--code", required=True, help="收款单编号（全局唯一）")
    p.add_argument("--amount", required=True, help="收款金额（元，正数，最多两位小数）")
    p.add_argument("--date", required=True, help="收款日期（YYYY-MM-DD，不得早于合同签订日期）")
    p.add_argument(
        "--milestone",
        help="归属里程碑编号（省略则计入合同级收款合计；指定时必须属于该合同）",
    )
    p.set_defaults(handler=cmd_record_payment)

    p = subparsers.add_parser(
        "query-contract",
        aliases=["show-contract"],
        help="查询合同及其全部变更单",
        description="按合同编号输出合同信息与全部变更单（按登记先后排列），不改动数据。",
    )
    p.add_argument("--code", required=True, help="合同编号")
    p.set_defaults(handler=cmd_query_contract)

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
            conn, args.contract, args.code, args.description, args.delta,
            milestone_code=args.milestone,
        )
    print(f"变更单已登记（草稿）：{code}")
    return 0


def cmd_register_milestone(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    with conn:  # 单事务：失败自动回滚
        code = register_milestone(
            conn, args.contract, args.code, args.name, args.amount, args.due
        )
    print(f"里程碑编号：{code}")
    return 0


def cmd_set_milestone_due(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    with conn:
        code, due_date = set_milestone_due(conn, args.code, args.due)
    print(f"里程碑编号：{code}")
    print(f"新到期日：{due_date}")
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


def cmd_record_payment(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    with conn:  # 单事务：失败自动回滚
        code, total_cents = register_payment(
            conn, args.contract, args.code, args.amount, args.date,
            milestone_code=args.milestone,
        )
    print(f"收款单编号：{code}")
    print(f"收款合计：{format_yuan(total_cents)}")
    return 0


def cmd_query_contract(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    contract, changes, milestones, payments = query_contract(conn, args.code)
    amount = current_cents(conn, contract["id"])
    print(f"合同编号：{contract['code']}")
    print(f"客户名称：{contract['customer']}")
    print(f"签订日期：{contract['signed_date']}")
    print(f"初始金额：{format_yuan(contract['initial_cents'])}")
    print(f"当前金额：{format_yuan(amount)}")
    if milestones:
        print("里程碑：")
        for ms in milestones:
            current = milestone_current_cents(conn, ms["id"])
            print(
                f"  - {ms['code']}  名称：{ms['name']}  "
                f"到期日：{ms['due_date']}  当前金额：{format_yuan(current)}"
            )
    else:
        print("里程碑：无")
    if changes:
        milestone_codes = {ms["id"]: ms["code"] for ms in milestones}
        print("变更单：")
        for ch in changes:
            if ch["milestone_id"] is None:
                attribution = "归属：合同"
            else:
                attribution = f"归属：{milestone_codes[ch['milestone_id']]}"
            print(
                f"  - {ch['code']}  变动：{format_yuan(ch['delta_cents'])}  "
                f"状态：{STATUS_LABELS[ch['status']]}  {attribution}"
            )
    else:
        print("变更单：无")
    if payments:
        milestone_codes = {ms["id"]: ms["code"] for ms in milestones}
        total = contract_payment_sum(conn, contract["id"])
        print(f"收款合计：{format_yuan(total)}")
        for pm in payments:
            if pm["milestone_id"] is None:
                attribution = "归属：合同"
            else:
                attribution = f"归属：{milestone_codes[pm['milestone_id']]}"
            print(
                f"  - {pm['code']}  金额：{format_yuan(pm['amount_cents'])}  "
                f"日期：{pm['paid_date']}  {attribution}"
            )
    else:
        print("收款：无")
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
