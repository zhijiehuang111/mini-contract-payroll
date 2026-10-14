"""資料庫連線。"""

import os

import psycopg
from psycopg.rows import DictRow, dict_row

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://payroll:payroll@localhost:5435/payroll"
)


# 每一列都是 dict（例如 row["pay_run_id"]）的連線
Conn = psycopg.Connection[DictRow]


def connect(url: str = DATABASE_URL) -> Conn:
    """autocommit=True：每個 `with conn.transaction():` 都是一個真的 transaction，
    區塊結束就 commit，出例外就 rollback。不在區塊裡的單一 SQL 各自立刻 commit。
    """
    return Conn.connect(url, autocommit=True, row_factory=dict_row)
