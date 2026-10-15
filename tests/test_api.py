"""API 的整合測試（連資料庫），對應 SPEC 第 7 節和第 9 節「整合」的 Idempotency-Key。"""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from payroll.api import app, get_conn
from payroll.worker import claim_pay_run, process_pay_run


@pytest.fixture
def client(conn):
    """API 改用測試的連線，跟測試本身看到的是同一個資料庫。"""
    app.dependency_overrides[get_conn] = lambda: conn
    yield TestClient(app)
    app.dependency_overrides.clear()


def create(client, period: str, key: str):
    return client.post(
        "/pay-runs", json={"period": period}, headers={"Idempotency-Key": key}
    )


def pay_run_count(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM pay_runs").fetchone()["n"]


# ---------- POST /pay-runs ----------


def test_create_returns_202_pending(client, conn):
    res = create(client, "2026-03", "k1")

    assert res.status_code == 202
    assert res.json()["period"] == "2026-03"
    assert res.json()["status"] == "pending"
    assert pay_run_count(conn) == 1


def test_same_key_twice_returns_existing_with_200(client, conn):
    """網路重試：同一個請求送兩次 → 只有一個 pay run，第二次回 200 和同一筆。"""
    first = create(client, "2026-03", "k1")
    second = create(client, "2026-03", "k1")

    assert second.status_code == 200
    assert second.json()["pay_run_id"] == first.json()["pay_run_id"]
    assert pay_run_count(conn) == 1


def test_same_key_returns_current_status(client, conn):
    """相同的 key 回傳的是那筆 pay run 現在的狀態，不是建立當下的。"""
    create(client, "2026-03", "k1")
    conn.execute("UPDATE pay_runs SET status = 'done'")

    res = create(client, "2026-03", "k1")

    assert res.status_code == 200
    assert res.json()["status"] == "done"


def test_new_key_same_month_returns_409_with_existing_id(client, conn):
    """兩個人各自按了「跑 3 月薪資」→ 第二個拿到 409 和既有的 id。"""
    first = create(client, "2026-03", "k1")
    second = create(client, "2026-03", "k2")

    assert second.status_code == 409
    assert second.json()["detail"]["pay_run_id"] == first.json()["pay_run_id"]
    assert pay_run_count(conn) == 1


def test_different_months_are_separate_pay_runs(client, conn):
    create(client, "2026-03", "k1")
    res = create(client, "2026-04", "k2")

    assert res.status_code == 202
    assert pay_run_count(conn) == 2


def test_missing_idempotency_key_returns_422(client):
    res = client.post("/pay-runs", json={"period": "2026-03"})

    assert res.status_code == 422


@pytest.mark.parametrize("period", ["2026-13", "2026-3", "202603", "march"])
def test_invalid_period_returns_422(client, period):
    assert create(client, period, "k1").status_code == 422


# ---------- GET ----------


def test_get_pay_run(client):
    pay_run_id = create(client, "2026-03", "k1").json()["pay_run_id"]

    res = client.get(f"/pay-runs/{pay_run_id}")

    assert res.status_code == 200
    assert res.json()["status"] == "pending"


@pytest.mark.parametrize("path", ["/pay-runs/999", "/pay-runs/999/payslips"])
def test_unknown_pay_run_returns_404(client, path):
    assert client.get(path).status_code == 404


def test_payslips_after_worker_runs(client, conn):
    """API 建立 → worker 算完 → API 查得到薪資單和明細。"""
    alice = conn.execute(
        "INSERT INTO contractors (name) VALUES ('Alice') RETURNING contractor_id"
    ).fetchone()["contractor_id"]
    conn.execute(
        """
        INSERT INTO contracts (contractor_id, salary_type, salary_amount, start_date)
        VALUES (%s, 'monthly', 50000, %s)
        """,
        (alice, date(2026, 3, 10)),
    )
    pay_run_id = create(client, "2026-03", "k1").json()["pay_run_id"]
    assert client.get(f"/pay-runs/{pay_run_id}/payslips").json() == []

    run = claim_pay_run(conn)
    assert run is not None
    process_pay_run(conn, run)

    assert client.get(f"/pay-runs/{pay_run_id}").json()["status"] == "done"
    payslips = client.get(f"/pay-runs/{pay_run_id}/payslips").json()
    assert len(payslips) == 1
    assert payslips[0]["contractor_id"] == alice
    assert payslips[0]["amount"] == "36667.00"
    assert payslips[0]["lines"][0]["paid_days"] == 22
