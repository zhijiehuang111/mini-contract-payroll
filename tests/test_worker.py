"""Worker 的整合測試（連資料庫），對應 SPEC 第 8 節和第 9 節「整合」裡跟重跑有關的項目。

API 相關的（Idempotency-Key、finalize 後重算 409、R2 的 422）在第 4 步做。
"""

from datetime import date
from decimal import Decimal

import pytest

from payroll import worker
from payroll.worker import PayRun, claim_pay_run, process_pay_run

# ---------- 準備資料的小工具 ----------


def add_contractor(conn, name: str) -> int:
    return conn.execute(
        "INSERT INTO contractors (name) VALUES (%s) RETURNING contractor_id", (name,)
    ).fetchone()["contractor_id"]


def add_contract(
    conn,
    contractor_id: int,
    salary_type: str,
    amount: int,
    start: date,
    end: date | None = None,
) -> int:
    return conn.execute(
        """
        INSERT INTO contracts (contractor_id, salary_type, salary_amount, start_date, end_date)
        VALUES (%s, %s, %s, %s, %s) RETURNING contract_id
        """,
        (contractor_id, salary_type, amount, start, end),
    ).fetchone()["contract_id"]


def add_hours(conn, contract_id: int, work_date: date, hours: str) -> None:
    conn.execute(
        "INSERT INTO timesheets (contract_id, work_date, hours) VALUES (%s, %s, %s)",
        (contract_id, work_date, Decimal(hours)),
    )


def add_pay_run(conn, year: int = 2026, month: int = 3, key: str = "k1") -> int:
    return conn.execute(
        """
        INSERT INTO pay_runs (idempotency_key, period_year, period_month, status)
        VALUES (%s, %s, %s, 'pending') RETURNING pay_run_id
        """,
        (key, year, month),
    ).fetchone()["pay_run_id"]


def mark_pending(conn, pay_run_id: int) -> None:
    """模擬「全部重算」：第 4 步的 API 也是把狀態設回 pending，交給 worker。"""
    conn.execute(
        "UPDATE pay_runs SET status = 'pending' WHERE pay_run_id = %s", (pay_run_id,)
    )


def claim_and_process(conn) -> PayRun:
    run = claim_pay_run(conn)
    assert run is not None
    process_pay_run(conn, run)
    return run


def status_of(conn, pay_run_id: int) -> dict:
    return conn.execute(
        "SELECT status, error FROM pay_runs WHERE pay_run_id = %s", (pay_run_id,)
    ).fetchone()


def payslips(conn, pay_run_id: int) -> dict[int, Decimal]:
    """{contractor_id: 金額}"""
    rows = conn.execute(
        "SELECT contractor_id, amount FROM payslips WHERE pay_run_id = %s",
        (pay_run_id,),
    ).fetchall()
    return {row["contractor_id"]: row["amount"] for row in rows}


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


@pytest.fixture
def march_staff(conn) -> dict[str, int]:
    """三個人：整月月薪、3/10 到職的月薪、時薪有工時。"""
    alice = add_contractor(conn, "Alice")
    add_contract(conn, alice, "monthly", 50000, date(2026, 1, 1))

    bob = add_contractor(conn, "Bob")
    add_contract(conn, bob, "monthly", 50000, date(2026, 3, 10))

    carol = add_contractor(conn, "Carol")
    carol_contract = add_contract(conn, carol, "hourly", 183, date(2026, 1, 1))
    add_hours(conn, carol_contract, date(2026, 3, 2), "7.5")
    add_hours(conn, carol_contract, date(2026, 3, 3), "8")
    add_hours(conn, carol_contract, date(2026, 4, 1), "8")  # 不是 3 月，不算

    return {
        "alice": alice,
        "bob": bob,
        "carol": carol,
        "carol_contract": carol_contract,
    }


# ---------- 正常跑一次 ----------


def test_process_writes_payslips_and_lines(conn, march_staff):
    run_id = add_pay_run(conn)

    claim_and_process(conn)

    assert status_of(conn, run_id) == {"status": "done", "error": None}
    assert payslips(conn, run_id) == {
        march_staff["alice"]: Decimal(50000),
        march_staff["bob"]: Decimal(36667),  # 50000 × 22 / 30
        march_staff["carol"]: Decimal(2837),  # 183 × 15.5 = 2836.5 → 2837
    }
    assert count(conn, "payslip_lines") == 3


def test_hourly_without_hours_needs_review(conn):
    dave = add_contractor(conn, "Dave")
    add_contract(conn, dave, "hourly", 200, date(2026, 1, 1))
    run_id = add_pay_run(conn)

    claim_and_process(conn)

    row = conn.execute(
        "SELECT amount, needs_review FROM payslips WHERE pay_run_id = %s", (run_id,)
    ).fetchone()
    assert row == {"amount": Decimal(0), "needs_review": True}


# ---------- 放心重跑 ----------


def test_run_twice_gives_same_result(conn, march_staff):
    """同一個 pay run 跑兩次 → 薪資單數量不變、金額相同、明細沒有變兩倍。"""
    run_id = add_pay_run(conn)
    claim_and_process(conn)
    first = payslips(conn, run_id)

    mark_pending(conn, run_id)
    claim_and_process(conn)

    assert payslips(conn, run_id) == first
    assert count(conn, "payslips") == 3
    assert count(conn, "payslip_lines") == 3


def test_crash_midway_then_retry(conn, march_staff, monkeypatch):
    """算到第二個人時丟例外：

    - 狀態變成 failed，error 記下原因
    - 第一個人已經 commit，留著；第二個人整個 rollback，沒有寫一半的薪資單
    - 修好之後重跑 → done，三個人都有、沒有重複
    """
    run_id = add_pay_run(conn)
    real_calculate = worker.calculate_payslip

    def broken_for_bob(year, month, contracts, hours_by_contract):
        payslip = real_calculate(year, month, contracts, hours_by_contract)
        assert payslip is not None
        if payslip.amount == Decimal(36667):  # Bob
            raise RuntimeError("boom")
        return payslip

    monkeypatch.setattr(worker, "calculate_payslip", broken_for_bob)
    claim_and_process(conn)

    status = status_of(conn, run_id)
    assert status["status"] == "failed"
    assert "boom" in status["error"]
    assert payslips(conn, run_id) == {march_staff["alice"]: Decimal(50000)}

    monkeypatch.setattr(worker, "calculate_payslip", real_calculate)
    mark_pending(conn, run_id)  # failed → 全部重算（SPEC 第 6 節）
    claim_and_process(conn)

    assert status_of(conn, run_id) == {"status": "done", "error": None}
    assert len(payslips(conn, run_id)) == 3
    assert count(conn, "payslip_lines") == 3


def test_contract_change_is_picked_up_on_rerun(conn, march_staff):
    """改了時薪合約的工時再重跑 → 薪資單被覆蓋成新的金額，不是多一張。"""
    run_id = add_pay_run(conn)
    claim_and_process(conn)

    add_hours(conn, march_staff["carol_contract"], date(2026, 3, 4), "4.5")
    mark_pending(conn, run_id)
    claim_and_process(conn)

    assert payslips(conn, run_id)[march_staff["carol"]] == Decimal(3660)  # 183 × 20
    assert count(conn, "payslips") == 3


def test_r7_payslip_removed_when_contract_moves_out_of_month(conn, march_staff):
    """Bob 的到職日更正成 4/1 後全部重算 → Bob 的薪資單和明細都被刪掉。"""
    run_id = add_pay_run(conn)
    claim_and_process(conn)
    assert march_staff["bob"] in payslips(conn, run_id)

    conn.execute(
        "UPDATE contracts SET start_date = '2026-04-01' WHERE contractor_id = %s",
        (march_staff["bob"],),
    )
    mark_pending(conn, run_id)
    claim_and_process(conn)

    assert march_staff["bob"] not in payslips(conn, run_id)
    assert count(conn, "payslips") == 2
    assert count(conn, "payslip_lines") == 2


# ---------- 領 pay run ----------


def test_claim_returns_none_when_nothing_to_do(conn):
    assert claim_pay_run(conn) is None


def test_claim_sets_running(conn):
    run_id = add_pay_run(conn)

    run = claim_pay_run(conn)

    assert run == PayRun(run_id, 2026, 3)
    assert status_of(conn, run_id)["status"] == "running"
    assert claim_pay_run(conn) is None  # 已經是 running，不會再被領一次


@pytest.mark.parametrize(
    "started_minutes_ago, reclaimed",
    [(11, True), (1, False)],
)
def test_stuck_running_is_reclaimed_after_timeout(conn, started_minutes_ago, reclaimed):
    """worker 掛掉，run 卡在 running：超過 10 分鐘別的 worker 可以重新領，還沒超過就不行。"""
    run_id = add_pay_run(conn)
    conn.execute(
        """
        UPDATE pay_runs SET status = 'running',
               started_at = now() - make_interval(mins => %s)
        WHERE pay_run_id = %s
        """,
        (started_minutes_ago, run_id),
    )

    run = claim_pay_run(conn)

    assert (run is not None) == reclaimed


def test_two_workers_do_not_claim_the_same_run(conn, connect_another):
    """worker A 正在領 3 月（還沒 commit），worker B 同時領 → SKIP LOCKED 讓 B 跳過 3 月、領到 4 月，
    而且不會卡住等 A。"""
    march = add_pay_run(conn, month=3, key="march")
    april = add_pay_run(conn, month=4, key="april")

    worker_a = connect_another()
    worker_b = connect_another()
    worker_b.execute("SET lock_timeout = '1s'")  # 萬一被鎖住，測試會失敗而不是卡死

    with worker_a.transaction():
        worker_a.execute(
            "SELECT 1 FROM pay_runs WHERE pay_run_id = %s FOR UPDATE", (march,)
        )
        run_b = claim_pay_run(worker_b)

    assert run_b is not None
    assert run_b.pay_run_id == april
