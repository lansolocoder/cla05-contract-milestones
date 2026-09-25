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

#: 发票状态（字面值即存储值，查询时原样输出）。
INVOICE_UNPAID = "unpaid"
INVOICE_PAID = "paid"
INVOICE_VOID = "void"

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

        CREATE TABLE IF NOT EXISTS invoices (
            id            INTEGER PRIMARY KEY,
            code          TEXT NOT NULL UNIQUE,
            contract_id   INTEGER NOT NULL
                                  REFERENCES contracts(id),
            milestone_id  INTEGER NOT NULL
                                  REFERENCES milestones(id),
            amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
            issue_date    TEXT NOT NULL,
            status        TEXT NOT NULL
                                  CHECK (status IN ('unpaid', 'paid', 'void')),
            seq           INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS payments (
            id            INTEGER PRIMARY KEY,
            code          TEXT NOT NULL UNIQUE,
            invoice_id    INTEGER NOT NULL
                                  REFERENCES invoices(id),
            amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
            pay_date      TEXT NOT NULL,
            seq           INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS overpayments (
            id            INTEGER PRIMARY KEY,
            contract_id   INTEGER NOT NULL UNIQUE
                                  REFERENCES contracts(id),
            amount_cents  INTEGER NOT NULL DEFAULT 0
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
) -> tuple[sqlite3.Row, list[sqlite3.Row], list[sqlite3.Row]]:
    code = require_text(code, "合同编号")
    contract = get_contract(conn, code)
    if contract is None:
        raise LedgerError(f"合同编号不存在：{code}")
    changes = conn.execute(
        "SELECT * FROM changes WHERE contract_id = ? ORDER BY seq",
        (contract["id"],),
    ).fetchall()
    milestones = list_milestones(conn, contract["id"])
    return contract, changes, milestones


# ---------------------------------------------------------------------------
# 发票与收款（均在调用方提供的事务内执行）
# ---------------------------------------------------------------------------

def get_invoice(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM invoices WHERE code = ?", (code,)
    ).fetchone()


def get_payment(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM payments WHERE code = ?", (code,)
    ).fetchone()


def invoice_paid_cents(conn: sqlite3.Connection, invoice_id: int) -> int:
    """该发票下全部收款单的金额合计（整数分）。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE invoice_id = ?",
        (invoice_id,),
    ).fetchone()
    return int(row[0])


def contract_overpayment_cents(conn: sqlite3.Connection, contract_id: int) -> int:
    """该合同累计超额收款余额；从未发生超收时为 0。"""
    row = conn.execute(
        """
        SELECT COALESCE(
            (SELECT amount_cents FROM overpayments WHERE contract_id = ?), 0
        )
        """,
        (contract_id,),
    ).fetchone()
    return int(row[0])


def _add_overpayment(
    conn: sqlite3.Connection, contract_id: int, cents: int
) -> None:
    """把超额部分累加进合同的超额收款余额（Upsert，原子）。"""
    conn.execute(
        """
        INSERT INTO overpayments (contract_id, amount_cents)
        VALUES (?, ?)
        ON CONFLICT(contract_id) DO UPDATE
            SET amount_cents = amount_cents + excluded.amount_cents
        """,
        (contract_id, cents),
    )


def issue_invoice(
    conn: sqlite3.Connection,
    invoice_code: str,
    milestone_code: str,
    amount_text: str,
    issue_date_text: str,
) -> tuple[str, str, int]:
    """登记发票。

    发票号经 --contract 传入（沿用命令行参数命名）；合同由里程碑
    归属确定：里程碑必须存在，开票日期不得早于其所属合同签订日期。
    """
    invoice_code = require_text(invoice_code, "发票号")
    milestone_code = require_text(milestone_code, "里程碑编号")
    amount_cents = parse_amount(
        amount_text, field="发票金额", allow_negative=False
    )
    issue_date = parse_iso_date(issue_date_text, field="开票日期")
    milestone = get_milestone(conn, milestone_code)
    if milestone is None:
        raise LedgerError(f"里程碑编号不存在：{milestone_code}")
    contract = get_contract_by_id(conn, milestone["contract_id"])
    if get_invoice(conn, invoice_code) is not None:
        raise LedgerError(f"发票号已存在，不得重复登记：{invoice_code}")
    if issue_date < contract["signed_date"]:
        raise LedgerError(
            f"开票日期不得早于合同签订日期："
            f"{issue_date} 早于 {contract['signed_date']}"
        )
    next_seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM invoices WHERE contract_id = ?",
        (contract["id"],),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO invoices
            (code, contract_id, milestone_id, amount_cents, issue_date, status, seq)
        VALUES (?, ?, ?, ?, ?, 'unpaid', ?)
        """,
        (invoice_code, contract["id"], milestone["id"],
         amount_cents, issue_date, next_seq),
    )
    return invoice_code, milestone_code, amount_cents


def record_payment(
    conn: sqlite3.Connection,
    invoice_code: str,
    payment_code: str,
    amount_text: str,
    pay_date_text: str,
) -> sqlite3.Row:
    invoice_code = require_text(invoice_code, "发票号")
    payment_code = require_text(payment_code, "收款单号")
    amount_cents = parse_amount(
        amount_text, field="收款金额", allow_negative=False
    )
    pay_date = parse_iso_date(pay_date_text, field="收款日期")
    invoice = get_invoice(conn, invoice_code)
    if invoice is None:
        raise LedgerError(f"发票号不存在：{invoice_code}")
    if invoice["status"] == INVOICE_VOID:
        raise LedgerError(f"发票已作废，不可再登记收款：{invoice_code}")
    if pay_date < invoice["issue_date"]:
        raise LedgerError(
            f"收款日期不得早于发票日期：{pay_date} 早于 {invoice['issue_date']}"
        )
    if get_payment(conn, payment_code) is not None:
        raise LedgerError(f"收款单号已存在，不得重复登记：{payment_code}")

    paid_before = invoice_paid_cents(conn, invoice["id"])
    invoice_amount = int(invoice["amount_cents"])
    # 按发票金额入账：本笔最多补齐发票未收部分，其余转为合同超收余额。
    excess_cents = max(0, paid_before + amount_cents - invoice_amount)
    next_seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM payments WHERE invoice_id = ?",
        (invoice["id"],),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO payments (code, invoice_id, amount_cents, pay_date, seq)
        VALUES (?, ?, ?, ?, ?)
        """,
        (payment_code, invoice["id"], amount_cents, pay_date, next_seq),
    )
    if excess_cents > 0:
        _add_overpayment(conn, invoice["contract_id"], excess_cents)
    # 累计收款恰好等于或（因超收）大于发票金额，均立即置为 paid。
    if paid_before + amount_cents >= invoice_amount:
        conn.execute(
            "UPDATE invoices SET status = 'paid' WHERE id = ?", (invoice["id"],)
        )
    return conn.execute(
        "SELECT * FROM invoices WHERE id = ?", (invoice["id"],)
    ).fetchone()


def query_payment(
    conn: sqlite3.Connection, contract_code: str
) -> tuple[list[sqlite3.Row], int]:
    """返回合同下全部发票（登记先后）及合同累计超收余额。

    每行附带 paid_total_cents（该发票全部收款合计）、
    received_cents（已收金额，封顶发票金额）、
    over_cents（该发票对应的超收余额）。
    """
    contract_code = require_text(contract_code, "合同编号")
    contract = get_contract(conn, contract_code)
    if contract is None:
        raise LedgerError(f"合同编号不存在：{contract_code}")
    invoices = conn.execute(
        """
        SELECT i.*,
               COALESCE(SUM(p.amount_cents), 0) AS paid_total_cents,
               m.code AS milestone_code
          FROM invoices AS i
          JOIN milestones AS m ON m.id = i.milestone_id
          LEFT JOIN payments AS p ON p.invoice_id = i.id
         WHERE i.contract_id = ?
         GROUP BY i.id
         ORDER BY i.seq
        """,
        (contract["id"],),
    ).fetchall()
    overpayment = contract_overpayment_cents(conn, contract["id"])
    return invoices, overpayment


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
        "query-contract",
        aliases=["show-contract"],
        help="查询合同及其全部变更单",
        description="按合同编号输出合同信息与全部变更单（按登记先后排列），不改动数据。",
    )
    p.add_argument("--code", required=True, help="合同编号")
    p.set_defaults(handler=cmd_query_contract)

    p = subparsers.add_parser(
        "issue-invoice",
        help="登记发票",
        description=(
            "为已有里程碑登记一张发票。--contract 传发票号（全局唯一），"
            "合同由里程碑归属确定；开票日期不得早于该合同签订日期。"
        ),
    )
    p.add_argument("--contract", required=True, help="发票号（全局唯一）")
    p.add_argument("--milestone", required=True, help="所属里程碑编号（必须存在）")
    p.add_argument("--amount", required=True, help="发票金额（元，正数，最多两位小数）")
    p.add_argument("--date", required=True, help="开票日期（YYYY-MM-DD，不得早于合同签订日期）")
    p.set_defaults(handler=cmd_issue_invoice)

    p = subparsers.add_parser(
        "record-payment",
        help="登记收款",
        description=(
            "为已有发票登记一笔收款。收款单号全局唯一，收款日期不得早于"
            "发票日期；累计收款超出发票金额的部分转记为合同超额收款余额。"
        ),
    )
    p.add_argument("--invoice", required=True, help="发票号")
    p.add_argument("--code", required=True, help="收款单号（全局唯一）")
    p.add_argument("--amount", required=True, help="收款金额（元，正数，最多两位小数）")
    p.add_argument("--date", required=True, help="收款日期（YYYY-MM-DD，不得早于发票日期）")
    p.set_defaults(handler=cmd_record_payment)

    p = subparsers.add_parser(
        "query-payment",
        help="查询合同的发票与收款",
        description=(
            "按合同编号逐行输出每张发票的发票号、里程碑编号、金额、状态、"
            "已收金额与超收余额，末行输出合同累计超额收款余额。"
        ),
    )
    p.add_argument("--contract", required=True, help="合同编号")
    p.set_defaults(handler=cmd_query_payment)

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


def cmd_query_contract(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    contract, changes, milestones = query_contract(conn, args.code)
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
    return 0


def cmd_issue_invoice(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    with conn:  # 单事务：失败自动回滚
        invoice_code, milestone_code, amount_cents = issue_invoice(
            conn, args.contract, args.milestone, args.amount, args.date
        )
    print(f"发票号：{invoice_code}")
    print(f"里程碑编号：{milestone_code}")
    print(f"金额：{format_yuan(amount_cents)}")
    return 0


def cmd_record_payment(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    with conn:
        invoice = record_payment(conn, args.invoice, args.code, args.amount, args.date)
        paid_total = invoice_paid_cents(conn, invoice["id"])
        received = min(paid_total, int(invoice["amount_cents"]))
    print(f"收款单号：{args.code}")
    print(f"发票号：{invoice['code']}")
    print(f"状态：{invoice['status']}")
    print(f"已收金额：{format_yuan(received)}")
    return 0


def cmd_query_payment(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    invoices, overpayment = query_payment(conn, args.contract)
    if invoices:
        for inv in invoices:
            paid_total = int(inv["paid_total_cents"])
            received = min(paid_total, int(inv["amount_cents"]))
            over = max(0, paid_total - int(inv["amount_cents"]))
            print(
                f"  - 发票号：{inv['code']}  里程碑编号：{inv['milestone_code']}  "
                f"金额：{format_yuan(inv['amount_cents'])}  状态：{inv['status']}  "
                f"已收金额：{format_yuan(received)}  超收余额：{format_yuan(over)}"
            )
    else:
        print("发票：无")
    print(f"累计超额收款余额：{format_yuan(overpayment)}")
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
