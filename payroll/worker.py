"""Pay run 的背景 worker。狀態和重跑的設計見 SPEC 第 6、8 節。

一輪的流程：
1. claim_pay_run()：領一個 pending（或卡住的 running）pay run，設成 running
2. process_pay_run()：每個人各自一個 transaction 算薪資、寫回資料庫
3. 全部算完 → 刪掉不在範圍內的薪資單（R7）→ done；中途出例外 → failed

為什麼可以放心重跑：每個人的寫入都是「upsert 薪資單 → 刪掉舊明細 → 寫入新明細」，
做幾次結果都一樣（冪等）。跑到一半掛掉，已經 commit 的人重跑時會被覆蓋，
還沒 commit 的人整個 rollback，不會留下寫一半的薪資單。
"""

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from payroll.calc import Contract, calculate_payslip, month_range
from payroll.db import Conn, connect

log = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=10)  # running 超過這麼久沒結束，視為 worker 掛了


@dataclass(frozen=True)
class PayRun:
    pay_run_id: int
    year: int
    month: int


def claim_pay_run(conn: Conn, stale_after: timedelta = STALE_AFTER) -> PayRun | None:
    """領一個 pay run 來做，沒有可以做的就回傳 None。

    - FOR UPDATE 鎖住選到的那一列，SKIP LOCKED 讓別的 worker 跳過它、去領下一個，
      所以開多個 worker 也不會領到同一個 run，也不會互相卡住
    - 子查詢選出來、外層 UPDATE 成 running，在同一個 transaction 裡，
      commit 之後別人看到的就是 running，不會再被當成 pending 領走
    """
    with conn.transaction():
        row = conn.execute(
            """
            UPDATE pay_runs SET status = 'running', started_at = now()
            WHERE pay_run_id = (
                SELECT pay_run_id FROM pay_runs
                WHERE status = 'pending'
                   OR (status = 'running' AND started_at < now() - %(stale_after)s)
                ORDER BY pay_run_id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING pay_run_id, period_year, period_month
            """,
            {"stale_after": stale_after},
        ).fetchone()
    if row is None:
        return None
    return PayRun(row["pay_run_id"], row["period_year"], row["period_month"])


def contractors_in_scope(conn: Conn, year: int, month: int) -> list[int]:
    """R7：至少有一份合約和這個月有交集的人。"""
    first_day, last_day = month_range(year, month)
    rows = conn.execute(
        """
        SELECT DISTINCT contractor_id FROM contracts
        WHERE start_date <= %(last_day)s
          AND (end_date IS NULL OR end_date >= %(first_day)s)
        ORDER BY contractor_id
        """,
        {"first_day": first_day, "last_day": last_day},
    ).fetchall()
    return [row["contractor_id"] for row in rows]


def load_inputs(
    conn: Conn, contractor_id: int, year: int, month: int
) -> tuple[list[Contract], dict[int, Decimal]]:
    """撈出 calculate_payslip() 需要的資料：這個人和這個月有交集的合約、每份合約這個月的工時加總。"""
    first_day, last_day = month_range(year, month)
    params = {
        "contractor_id": contractor_id,
        "first_day": first_day,
        "last_day": last_day,
    }

    contract_rows = conn.execute(
        """
        SELECT contract_id, salary_type, salary_amount, start_date, end_date
        FROM contracts
        WHERE contractor_id = %(contractor_id)s
          AND start_date <= %(last_day)s
          AND (end_date IS NULL OR end_date >= %(first_day)s)
        ORDER BY start_date
        """,
        params,
    ).fetchall()
    contracts = [Contract(**row) for row in contract_rows]

    hour_rows = conn.execute(
        """
        SELECT t.contract_id, SUM(t.hours) AS hours
        FROM timesheets t
        JOIN contracts c USING (contract_id)
        WHERE c.contractor_id = %(contractor_id)s
          AND t.work_date BETWEEN %(first_day)s AND %(last_day)s
        GROUP BY t.contract_id
        """,
        params,
    ).fetchall()
    hours_by_contract = {row["contract_id"]: row["hours"] for row in hour_rows}

    return contracts, hours_by_contract


def recalculate_contractor(conn: Conn, run: PayRun, contractor_id: int) -> None:
    """算一個人的薪資單並寫回資料庫，整段是一個 transaction。

    第 4 步的「重算指定的人」也直接呼叫這個函式。
    """
    with conn.transaction():
        contracts, hours_by_contract = load_inputs(
            conn, contractor_id, run.year, run.month
        )
        payslip = calculate_payslip(run.year, run.month, contracts, hours_by_contract)

        if payslip is None:
            # R7：這個人這個月已經沒有合約了，有舊的薪資單就刪掉（明細 ON DELETE CASCADE）
            conn.execute(
                "DELETE FROM payslips WHERE pay_run_id = %s AND contractor_id = %s",
                (run.pay_run_id, contractor_id),
            )
            return

        # upsert：第一次跑是 INSERT；重跑撞到 UNIQUE (pay_run_id, contractor_id) 就改成 UPDATE
        row = conn.execute(
            """
            INSERT INTO payslips (pay_run_id, contractor_id, amount, needs_review)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (pay_run_id, contractor_id) DO UPDATE
            SET amount = EXCLUDED.amount, needs_review = EXCLUDED.needs_review
            RETURNING payslip_id
            """,
            (run.pay_run_id, contractor_id, payslip.amount, payslip.needs_review),
        ).fetchone()
        assert row is not None  # RETURNING 一定會回一列
        payslip_id = row["payslip_id"]

        # 明細沒有自然的唯一鍵（合約數可能變），所以整批刪掉重寫
        conn.execute("DELETE FROM payslip_lines WHERE payslip_id = %s", (payslip_id,))
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO payslip_lines (
                    payslip_id, contract_id, salary_type, salary_amount,
                    start_date, end_date, paid_days, divisor_days, work_hours, amount
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        payslip_id,
                        line.contract_id,
                        line.salary_type,
                        line.salary_amount,
                        line.start_date,
                        line.end_date,
                        line.paid_days,
                        line.divisor_days,
                        line.work_hours,
                        line.amount,
                    )
                    for line in payslip.lines
                ],
            )


def process_pay_run(conn: Conn, run: PayRun) -> None:
    """算完整個 pay run。成功設成 done，出例外設成 failed 並記下原因。"""
    try:
        contractor_ids = contractors_in_scope(conn, run.year, run.month)
        for contractor_id in contractor_ids:
            recalculate_contractor(conn, run, contractor_id)

        with conn.transaction():
            # R7：上一次跑有、這次已經不在範圍內的人（例如到職日更正成下個月）
            conn.execute(
                "DELETE FROM payslips WHERE pay_run_id = %s AND contractor_id <> ALL(%s)",
                (run.pay_run_id, contractor_ids),
            )
            conn.execute(
                "UPDATE pay_runs SET status = 'done', error = NULL WHERE pay_run_id = %s",
                (run.pay_run_id,),
            )
        log.info("pay run %s done: %d payslips", run.pay_run_id, len(contractor_ids))
    except Exception as e:
        log.exception("pay run %s failed", run.pay_run_id)
        conn.execute(
            "UPDATE pay_runs SET status = 'failed', error = %s WHERE pay_run_id = %s",
            (repr(e), run.pay_run_id),
        )


def run_forever(poll_seconds: float = 3) -> None:
    """worker 主迴圈：有 pay run 就做，沒有就睡一下再看。"""
    with connect() as conn:
        log.info("worker started")
        while True:
            run = claim_pay_run(conn)
            if run is None:
                time.sleep(poll_seconds)
                continue
            log.info(
                "claimed pay run %s (%d-%02d)", run.pay_run_id, run.year, run.month
            )
            process_pay_run(conn, run)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    run_forever()
