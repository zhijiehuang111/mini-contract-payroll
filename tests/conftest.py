"""整合測試用的資料庫。用獨立的 payroll_test，每個測試前整個重建，不會動到開發用的資料。"""

import os
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest

from payroll.db import DATABASE_URL, connect

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://payroll:payroll@localhost:5435/payroll_test"
)
# psycopg 只收 LiteralString，避免把使用者輸入拼進 SQL。schema.sql 是 repo 裡的檔案，可以放心 cast
SCHEMA = cast(LiteralString, (Path(__file__).parent.parent / "schema.sql").read_text())


@pytest.fixture(scope="session", autouse=False)
def test_database():
    """payroll_test 不存在就建一個。"""
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = 'payroll_test'"
        ).fetchone()
        if not exists:
            admin.execute("CREATE DATABASE payroll_test")
    return TEST_DATABASE_URL


@pytest.fixture
def conn(test_database):
    """乾淨的資料庫：砍掉 public schema 再跑一次 schema.sql。"""
    with connect(test_database) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute(SCHEMA)
        yield conn


@pytest.fixture
def connect_another(test_database):
    """開第二條連線，模擬另一個 worker。"""
    opened = []

    def _connect():
        c = connect(test_database)
        opened.append(c)
        return c

    yield _connect
    for c in opened:
        c.close()
