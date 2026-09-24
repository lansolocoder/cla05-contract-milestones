"""SQLite persistence and business rules for contracts and change orders."""

from __future__ import annotations

import datetime as _dt
import os
import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

# 数据库文件固定放在项目根目录（包目录的上一级），退出进程后数据仍然保留。
# 测试可通过 CONTRACT_LEDGER_DB 环境变量指向临时文件。
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "contract_ledger.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    contract_id TEXT PRIMARY KEY,
    customer TEXT NOT NULL,
    sign_date TEXT NOT NULL,
    initial_amount_cents INTEGER NOT NULL CHECK (initial_amount_cents > 0)
);
CREATE TABLE IF NOT EXISTS change_orders (
    change_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts (contract_id),
    description TEXT NOT NULL,
    delta_cents INTEGER NOT NULL CHECK (delta_cents != 0),
    status TEXT NOT NULL CHECK (status IN ('draft', 'applied', 'void'))
);
"""

STATUS_LABELS = {"draft": "草稿", "applied": "已生效", "void": "已作废"}

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_POSITIVE_AMOUNT_RE = re.compile(r"\d+(\.\d{1,2})?")
_DELTA_AMOUNT_RE = re.compile(r"[+-]?\d+(\.\d{1,2})?")


class LedgerError(Exception):
    """Business-rule violation; reported on stderr with a non-zero exit."""


@dataclass(frozen=True)
class ChangeOrder:
    change_id: str
    contract_id: str
    description: str
    delta_cents: int
    status: str


@dataclass(frozen=True)
class Contract:
    contract_id: str
    customer: str
    sign_date: str
    initial_amount_cents: int
    changes: tuple[ChangeOrder, ...]

    @property
    def current_amount_cents(self) -> int:
        applied = sum(c.delta_cents for c in self.changes if c.status == "applied")
        return self.initial_amount_cents + applied


def format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def format_signed_cents(cents: int) -> str:
    return ("+" if cents >= 0 else "") + format_cents(cents)


def parse_date(text: str) -> str:
    if not _DATE_RE.fullmatch(text):
        raise LedgerError(f"签订日期格式非法: {text!r}（应为 YYYY-MM-DD）")
    try:
        _dt.date.fromisoformat(text)
    except ValueError:
        raise LedgerError(f"签订日期不是有效日期: {text!r}") from None
    return text


def _to_cents(text: str) -> int:
    try:
        return int(Decimal(text) * 100)
    except InvalidOperation:
        raise LedgerError(f"金额格式非法: {text!r}") from None


def parse_initial_amount(text: str) -> int:
    if not _POSITIVE_AMOUNT_RE.fullmatch(text):
        raise LedgerError(f"初始金额格式非法: {text!r}（应为正数，最多两位小数）")
    cents = _to_cents(text)
    if cents <= 0:
        raise LedgerError(f"初始金额必须为正数: {text!r}")
    return cents


def parse_delta_amount(text: str) -> int:
    if not _DELTA_AMOUNT_RE.fullmatch(text):
        raise LedgerError(f"金额变动格式非法: {text!r}（最多两位小数，不得为零）")
    cents = _to_cents(text)
    if cents == 0:
        raise LedgerError("金额变动不得为零")
    return cents


def _require_text(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise LedgerError(f"{label}不能为空")
    return value


def connect(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    path = db_path or os.environ.get("CONTRACT_LEDGER_DB") or DEFAULT_DB_PATH
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


def _fetch_contract_row(conn: sqlite3.Connection, contract_id: str) -> sqlite3.Row | tuple:
    row = conn.execute(
        "SELECT contract_id, customer, sign_date, initial_amount_cents "
        "FROM contracts WHERE contract_id = ?",
        (contract_id,),
    ).fetchone()
    if row is None:
        raise LedgerError(f"合同不存在: {contract_id}")
    return row


def _fetch_change_row(conn: sqlite3.Connection, change_id: str) -> tuple:
    row = conn.execute(
        "SELECT change_id, contract_id, description, delta_cents, status "
        "FROM change_orders WHERE change_id = ?",
        (change_id,),
    ).fetchone()
    if row is None:
        raise LedgerError(f"变更单不存在: {change_id}")
    return row


def _current_amount_cents(conn: sqlite3.Connection, contract_id: str) -> int:
    row = conn.execute(
        "SELECT c.initial_amount_cents + COALESCE(SUM(o.delta_cents), 0) "
        "FROM contracts c LEFT JOIN change_orders o "
        "  ON o.contract_id = c.contract_id AND o.status = 'applied' "
        "WHERE c.contract_id = ? GROUP BY c.contract_id",
        (contract_id,),
    ).fetchone()
    return int(row[0])


def register_contract(
    conn: sqlite3.Connection,
    contract_id: str,
    customer: str,
    sign_date: str,
    initial_amount_cents: int,
) -> None:
    contract_id = _require_text(contract_id, "合同编号")
    customer = _require_text(customer, "客户名称")
    try:
        with conn:
            conn.execute(
                "INSERT INTO contracts (contract_id, customer, sign_date, "
                "initial_amount_cents) VALUES (?, ?, ?, ?)",
                (contract_id, customer, sign_date, initial_amount_cents),
            )
    except sqlite3.IntegrityError:
        raise LedgerError(f"合同编号已存在，不得重复登记: {contract_id}") from None


def register_change(
    conn: sqlite3.Connection,
    contract_id: str,
    change_id: str,
    description: str,
    delta_cents: int,
) -> None:
    contract_id = _require_text(contract_id, "合同编号")
    change_id = _require_text(change_id, "变更单编号")
    description = _require_text(description, "变更说明")
    with conn:
        _fetch_contract_row(conn, contract_id)
        try:
            conn.execute(
                "INSERT INTO change_orders (change_id, contract_id, description, "
                "delta_cents, status) VALUES (?, ?, ?, ?, 'draft')",
                (change_id, contract_id, description, delta_cents),
            )
        except sqlite3.IntegrityError:
            raise LedgerError(f"变更单编号已存在，不得重复登记: {change_id}") from None


def apply_change(conn: sqlite3.Connection, change_id: str) -> tuple[str, int]:
    """生效草稿变更单，返回 (合同编号, 生效后的当前金额)。"""
    change_id = _require_text(change_id, "变更单编号")
    with conn:
        row = _fetch_change_row(conn, change_id)
        _, contract_id, _, delta_cents, status = row
        if status == "applied":
            raise LedgerError(f"变更单已生效，不得重复生效: {change_id}")
        if status == "void":
            raise LedgerError(f"变更单已作废，不得再生效: {change_id}")
        new_amount = _current_amount_cents(conn, contract_id) + delta_cents
        if new_amount <= 0:
            raise LedgerError(
                f"生效后合同当前金额将为 {format_cents(new_amount)} 元，"
                "不大于零，拒绝生效"
            )
        conn.execute(
            "UPDATE change_orders SET status = 'applied' WHERE change_id = ?",
            (change_id,),
        )
        return contract_id, new_amount


def void_change(conn: sqlite3.Connection, change_id: str) -> tuple[str, int]:
    """作废变更单，返回 (合同编号, 作废后的当前金额)。"""
    change_id = _require_text(change_id, "变更单编号")
    with conn:
        row = _fetch_change_row(conn, change_id)
        _, contract_id, _, delta_cents, status = row
        if status == "void":
            raise LedgerError(f"变更单已作废，不得重复作废: {change_id}")
        new_amount = _current_amount_cents(conn, contract_id)
        if status == "applied":
            new_amount -= delta_cents
            if new_amount <= 0:
                raise LedgerError(
                    f"作废后合同当前金额将为 {format_cents(new_amount)} 元，"
                    "不大于零，拒绝作废"
                )
        conn.execute(
            "UPDATE change_orders SET status = 'void' WHERE change_id = ?",
            (change_id,),
        )
        return contract_id, new_amount


def get_contract(conn: sqlite3.Connection, contract_id: str) -> Contract:
    row = _fetch_contract_row(conn, contract_id)
    changes = conn.execute(
        "SELECT change_id, contract_id, description, delta_cents, status "
        "FROM change_orders WHERE contract_id = ? ORDER BY rowid",
        (contract_id,),
    ).fetchall()
    return Contract(
        contract_id=row[0],
        customer=row[1],
        sign_date=row[2],
        initial_amount_cents=row[3],
        changes=tuple(ChangeOrder(*c) for c in changes),
    )
