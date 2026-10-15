"""HTTP API。規格見 SPEC 第 7 節，迷你版只做建立 pay run 和查詢。

API 只負責把 pay run 寫成 pending，真正的計算交給 worker（payroll/worker.py）。
"""

from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

from payroll.db import Conn, connect

app = FastAPI(title="Mini Contract Payroll")


def get_conn() -> Iterator[Conn]:
    """每個 request 開一條連線，結束就關掉。測試會換成連 payroll_test 的版本。"""
    with connect() as conn:
        yield conn


ConnDep = Annotated[Conn, Depends(get_conn)]


# ---------- 輸入和輸出的格式 ----------


class PayRunCreate(BaseModel):
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", examples=["2026-03"])


class PayRunOut(BaseModel):
    pay_run_id: int
    period: str
    status: str
    error: str | None
    started_at: datetime | None
    created_at: datetime


class PayslipLineOut(BaseModel):
    contract_id: int
    salary_type: str
    salary_amount: Decimal
    start_date: date
    end_date: date
    paid_days: int | None
    divisor_days: int | None
    work_hours: Decimal | None
    amount: Decimal


class PayslipOut(BaseModel):
    payslip_id: int
    contractor_id: int
    amount: Decimal
    needs_review: bool
    lines: list[PayslipLineOut]


PAY_RUN_COLUMNS = """
    pay_run_id,
    period_year || '-' || lpad(period_month::text, 2, '0') AS period,
    status, error, started_at, created_at
"""


def find_pay_run(
    conn: Conn, column: Literal["pay_run_id", "idempotency_key"], value: int | str
) -> PayRunOut | None:
    row = conn.execute(
        f"SELECT {PAY_RUN_COLUMNS} FROM pay_runs WHERE {column} = %s",
        (value,),
    ).fetchone()
    return PayRunOut(**row) if row else None


# ---------- 路由 ----------


@app.post(
    "/pay-runs",
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"description": "同一個 Idempotency-Key 重送，回傳之前那筆"}},
)
def create_pay_run(
    body: PayRunCreate,
    idempotency_key: Annotated[str, Header(min_length=1)],
    conn: ConnDep,
    response: Response,
) -> PayRunOut:
    """建立 pay run，交給 worker 計算。

    - 新的 key、這個月還沒有 pay run → 202，建立一筆 pending
    - 相同的 key 已經存在 → 200，直接回傳之前那筆（不管它現在是什麼狀態）
    - 新的 key，但這個月已經有 pay run → 409，回傳既有 pay run 的 id
    """
    year, month = (int(part) for part in body.period.split("-"))

    # 兩個 UNIQUE（idempotency_key、年月）撞到任何一個都不新增，交給下面判斷是哪一種。
    # 用資料庫的 UNIQUE 擋，而不是先 SELECT 再 INSERT：兩個請求同時進來時，
    # 先查再寫的話兩邊都會查到「沒有」，然後都寫進去
    inserted = conn.execute(
        """
        INSERT INTO pay_runs (idempotency_key, period_year, period_month, status)
        VALUES (%s, %s, %s, 'pending')
        ON CONFLICT DO NOTHING
        RETURNING pay_run_id
        """,
        (idempotency_key, year, month),
    ).fetchone()
    if inserted:
        pay_run = find_pay_run(conn, "pay_run_id", inserted["pay_run_id"])
        assert pay_run is not None
        return pay_run

    same_key = find_pay_run(conn, "idempotency_key", idempotency_key)
    if same_key:
        response.status_code = status.HTTP_200_OK
        return same_key

    existing = conn.execute(
        "SELECT pay_run_id FROM pay_runs WHERE period_year = %s AND period_month = %s",
        (year, month),
    ).fetchone()
    assert existing is not None  # 不是 key 撞到，就一定是年月撞到
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        {
            "message": f"pay run for {body.period} already exists",
            "pay_run_id": existing["pay_run_id"],
        },
    )


@app.get("/pay-runs/{pay_run_id}")
def get_pay_run(pay_run_id: int, conn: ConnDep) -> PayRunOut:
    pay_run = find_pay_run(conn, "pay_run_id", pay_run_id)
    if pay_run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pay run not found")
    return pay_run


@app.get("/pay-runs/{pay_run_id}/payslips")
def list_payslips(pay_run_id: int, conn: ConnDep) -> list[PayslipOut]:
    """這個 pay run 的所有薪資單和明細。worker 還沒算完時可能是空的或只有一部分。"""
    if find_pay_run(conn, "pay_run_id", pay_run_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pay run not found")

    payslips = conn.execute(
        """
        SELECT payslip_id, contractor_id, amount, needs_review
        FROM payslips WHERE pay_run_id = %s ORDER BY contractor_id
        """,
        (pay_run_id,),
    ).fetchall()
    lines = conn.execute(
        """
        SELECT l.payslip_id, l.contract_id, l.salary_type, l.salary_amount,
               l.start_date, l.end_date, l.paid_days, l.divisor_days, l.work_hours, l.amount
        FROM payslip_lines l
        JOIN payslips p USING (payslip_id)
        WHERE p.pay_run_id = %s
        ORDER BY l.start_date
        """,
        (pay_run_id,),
    ).fetchall()

    lines_by_payslip: dict[int, list[PayslipLineOut]] = {}
    for line in lines:
        payslip_id = line.pop("payslip_id")
        lines_by_payslip.setdefault(payslip_id, []).append(PayslipLineOut(**line))

    return [
        PayslipOut(**p, lines=lines_by_payslip.get(p["payslip_id"], []))
        for p in payslips
    ]
