"""SQLite 持久化层与并发控制。

并发策略
--------
服务以单容器多线程方式运行（Docker Compose），并发提交分两层串行化：

1. 进程内 ``threading.RLock``：同一月份的“读取已确认合计 -> 可行性求解 ->
   写入新合计”临界区在进程内互斥；
2. SQLite ``BEGIN IMMEDIATE``：事务一开始即获取写锁，若未来多进程共享
   同一数据库文件，写事务同样被数据库层串行化，锁忙时按 busy_timeout
   等待而不是直接报错。

只有同一批次与已确认合计被求解器判定为同时可行时才提交事务；无解时
回滚并保留已确认合计。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from . import solver
from .solver import COL, GRAND, ROW, ConstraintKey

DEFAULT_DB_PATH = os.environ.get(
    "ROUNDING_DB_PATH", "/data/rounding.db"
)

# 进程内写锁，保证 check-then-write 临界区原子。
_write_lock = threading.RLock()


class TableExistsError(RuntimeError):
    """该月份矩阵已登记（矩阵登记后不可修改）。"""


class MonthNotFoundError(LookupError):
    """该月份尚未登记矩阵。"""


class RequestShapeError(ValueError):
    """请求体尺寸与已登记矩阵不一致（对应 HTTP 422）。"""


class TotalsConflictError(RuntimeError):
    """本批合计与已确认合计无法被同一张取整表同时满足。"""

    def __init__(self, reasons: list[str], confirmed: list[dict]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons
        self.confirmed = confirmed


def get_db_path() -> str:
    """每次调用读取环境变量，便于测试 monkeypatch 后即时生效。"""
    return os.environ.get("ROUNDING_DB_PATH", DEFAULT_DB_PATH)


def _connect() -> sqlite3.Connection:
    path = get_db_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def _immediate_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """开启立即写锁事务，异常即回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def init_db() -> None:
    """建表（幂等），应用启动时调用。"""
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS matrices (
                month        TEXT PRIMARY KEY,
                rows         INTEGER NOT NULL,
                cols         INTEGER NOT NULL,
                row_names    TEXT NOT NULL,
                col_names    TEXT NOT NULL,
                matrix_json  TEXT NOT NULL,
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS confirmed_totals (
                month       TEXT NOT NULL,
                kind        TEXT NOT NULL CHECK (kind IN ('row','col','grand')),
                pos         INTEGER NOT NULL,
                value       INTEGER NOT NULL,
                batch_no    INTEGER NOT NULL,
                batch_label TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (month, kind, pos),
                FOREIGN KEY (month) REFERENCES matrices(month)
            );

            CREATE TABLE IF NOT EXISTS batches (
                month       TEXT NOT NULL,
                batch_no    INTEGER NOT NULL,
                label       TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (month, batch_no),
                FOREIGN KEY (month) REFERENCES matrices(month)
            );
            """
        )


# ---------------------------------------------------------------------------
# 矩阵登记
# ---------------------------------------------------------------------------

def register_table(
    month: str,
    matrix: list[list[int]],
    row_names: list[str],
    col_names: list[str],
) -> dict:
    """登记某月矩阵；已存在则抛出 TableExistsError（登记后不可修改）。"""
    with _write_lock, _connect() as conn:
        existing = conn.execute(
            "SELECT month FROM matrices WHERE month = ?", (month,)
        ).fetchone()
        if existing is not None:
            raise TableExistsError(
                f"{month} 的矩阵已登记，登记后不可修改；如需更正请走数据治理流程"
            )
        conn.execute(
            "INSERT INTO matrices (month, rows, cols, row_names, col_names, matrix_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                month,
                len(matrix),
                len(matrix[0]),
                json.dumps(row_names, ensure_ascii=False),
                json.dumps(col_names, ensure_ascii=False),
                json.dumps(matrix),
            ),
        )
    legal = solver.legal_totals(matrix)
    return {
        "status": "registered",
        "month": month,
        "dimensions": {"rows": len(matrix), "cols": len(matrix[0])},
        "row_names": row_names,
        "col_names": col_names,
        "matrix": matrix,
        "legal_row_ranges": legal["row_ranges"],
        "legal_col_ranges": legal["col_ranges"],
        "legal_grand_range": list(legal["grand_range"]),
    }


def _load_matrix(conn: sqlite3.Connection, month: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM matrices WHERE month = ?", (month,)
    ).fetchone()
    if row is None:
        raise MonthNotFoundError(f"月份 {month} 尚未登记矩阵")
    return row


def _decode_table(row: sqlite3.Row) -> dict:
    return {
        "month": row["month"],
        "rows": row["rows"],
        "cols": row["cols"],
        "row_names": json.loads(row["row_names"]),
        "col_names": json.loads(row["col_names"]),
        "matrix": json.loads(row["matrix_json"]),
    }


# ---------------------------------------------------------------------------
# 已确认合计读写
# ---------------------------------------------------------------------------

def _read_confirmed(conn: sqlite3.Connection, month: str) -> dict[ConstraintKey, int]:
    result: dict[ConstraintKey, int] = {}
    for r in conn.execute(
        "SELECT kind, pos, value FROM confirmed_totals WHERE month = ?",
        (month,),
    ):
        result[(r["kind"], r["pos"])] = r["value"]
    return result


def _confirmed_to_items(
    confirmed: dict[ConstraintKey, int],
    row_names: list[str],
    col_names: list[str],
) -> list[dict]:
    items: list[dict] = []
    for i, name in enumerate(row_names):
        if (ROW, i) in confirmed:
            items.append({"kind": ROW, "index": i, "name": name,
                          "value": confirmed[(ROW, i)]})
    for j, name in enumerate(col_names):
        if (COL, j) in confirmed:
            items.append({"kind": COL, "index": j, "name": name,
                          "value": confirmed[(COL, j)]})
    if (GRAND, 0) in confirmed:
        items.append({"kind": GRAND, "index": 0, "name": "grand_total",
                      "value": confirmed[(GRAND, 0)]})
    return items


def submit_batch(
    month: str,
    row_totals: list[int | None],
    col_totals: list[int | None],
    grand_total: int | None,
    batch_label: str | None,
) -> dict:
    """提交一批合计；可行才写入，冲突抛出 TotalsConflictError。

    与已确认值完全相同的合计按幂等处理（不重复计数）。
    """
    with _write_lock, _connect() as conn:
        table_row = _load_matrix(conn, month)
        table = _decode_table(table_row)
        matrix = table["matrix"]
        row_names, col_names = table["row_names"], table["col_names"]

        if len(row_totals) != table["rows"] or len(col_totals) != table["cols"]:
            raise RequestShapeError(
                f"合计数组长度与 {month} 已登记的 {table['rows']}x"
                f"{table['cols']} 矩阵尺寸不一致"
            )

        # 本批携带的全部候选约束（含与已确认相同的幂等项）。
        candidate: dict[ConstraintKey, int] = {}
        for i, value in enumerate(row_totals):
            if value is not None:
                candidate[(ROW, i)] = value
        for j, value in enumerate(col_totals):
            if value is not None:
                candidate[(COL, j)] = value
        if grand_total is not None:
            candidate[(GRAND, 0)] = grand_total

        # 已确认值不可更改：同一下标出现不同值直接冲突，无需调用求解器。
        confirmed = _read_confirmed(conn, month)
        immutable_conflicts: list[str] = []
        for key, value in candidate.items():
            if key in confirmed and confirmed[key] != value:
                kind, pos = key
                label = {ROW: f"第 {pos} 行", COL: f"第 {pos} 列",
                         GRAND: "全表"}[kind]
                immutable_conflicts.append(
                    f"{label}合计已确认为 {confirmed[key]}，已确认的数不能改，"
                    f"本批提交的 {value} 被拒绝"
                )
        if immutable_conflicts:
            raise TotalsConflictError(
                immutable_conflicts,
                _confirmed_to_items(confirmed, row_names, col_names),
            )

        merged = {**confirmed, **candidate}
        witness, reasons = solver.solve(matrix, merged)
        if witness is None:
            raise TotalsConflictError(
                reasons,
                _confirmed_to_items(confirmed, row_names, col_names),
            )

        # 可行：写入新确认项与批次记录。
        next_batch = conn.execute(
            "SELECT COALESCE(MAX(batch_no), 0) + 1 FROM batches WHERE month = ?",
            (month,),
        ).fetchone()[0]
        newly: list[dict] = []
        for key, value in candidate.items():
            if key in confirmed:
                continue  # 幂等：值相同，不重复写入
            kind, pos = key
            conn.execute(
                "INSERT INTO confirmed_totals (month, kind, pos, value, batch_no, batch_label)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (month, kind, pos, value, next_batch, batch_label),
            )
            name = (
                row_names[pos] if kind == ROW
                else col_names[pos] if kind == COL
                else "grand_total"
            )
            newly.append({"kind": kind, "index": pos, "name": name, "value": value})
        conn.execute(
            "INSERT INTO batches (month, batch_no, label) VALUES (?, ?, ?)",
            (month, next_batch, batch_label),
        )

        final_confirmed = _read_confirmed(conn, month)
        row_sums = [sum(witness[i]) for i in range(len(witness))]
        col_sums = [
            sum(witness[i][j] for i in range(len(witness)))
            for j in range(len(witness[0]))
        ]
        return {
            "status": "accepted",
            "month": month,
            "batch_no": next_batch,
            "newly_confirmed": newly,
            "confirmed_totals": _confirmed_to_items(
                final_confirmed, row_names, col_names
            ),
            "witness_matrix": witness,
            "witness_row_totals": row_sums,
            "witness_col_totals": col_sums,
            "witness_grand_total": sum(row_sums),
        }


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def get_month(month: str) -> dict:
    """返回当月原表、符合已确认合计的取整矩阵及其加出的合计。"""
    with _connect() as conn:
        table = _decode_table(_load_matrix(conn, month))
        confirmed = _read_confirmed(conn, month)

    witness, reasons = solver.solve(table["matrix"], confirmed)
    if witness is None:  # 正常不会发生：写入时已保证可行
        raise RuntimeError(f"内部错误：{month} 的已确认合计无解：{reasons}")
    row_sums = [sum(witness[i]) for i in range(len(witness))]
    col_sums = [
        sum(witness[i][j] for i in range(len(witness)))
        for j in range(len(witness[0]))
    ]
    return {
        "status": "ok",
        "month": month,
        "dimensions": {"rows": table["rows"], "cols": table["cols"]},
        "row_names": table["row_names"],
        "col_names": table["col_names"],
        "original_matrix": table["matrix"],
        "rounded_matrix": witness,
        "row_totals": row_sums,
        "col_totals": col_sums,
        "grand_total": sum(row_sums),
        "confirmed_totals": _confirmed_to_items(
            confirmed, table["row_names"], table["col_names"]
        ),
    }


def list_months() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT m.month, m.rows, m.cols,
                   (SELECT COUNT(*) FROM confirmed_totals c WHERE c.month = m.month) AS confirmed_count,
                   (SELECT COUNT(*) FROM batches b WHERE b.month = m.month) AS batches
            FROM matrices m ORDER BY m.month
            """
        ).fetchall()
    return [
        {
            "month": r["month"],
            "dimensions": {"rows": r["rows"], "cols": r["cols"]},
            "confirmed_count": r["confirmed_count"],
            "batches": r["batches"],
        }
        for r in rows
    ]


def get_confirmed(month: str) -> list[dict]:
    with _connect() as conn:
        table = _decode_table(_load_matrix(conn, month))
        confirmed = _read_confirmed(conn, month)
    return _confirmed_to_items(
        confirmed, table["row_names"], table["col_names"]
    )
