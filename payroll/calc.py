"""薪資計算（純函式，不連資料庫）。規則見 SPEC 第 4 節。

資料流：worker 從資料庫撈出「某個人跟這個月有交集的合約」和「時薪合約這個月的工時加總」，
呼叫 calculate_payslip()，拿到 Payslip 之後再寫回 payslips / payslip_lines。
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

SalaryType = Literal["monthly", "hourly"]

DIVISOR_DAYS = 30  # R1：30 天制


@dataclass(frozen=True)
class Contract:
    """對應 contracts 表的一列。"""

    contract_id: int
    salary_type: SalaryType
    salary_amount: Decimal  # 月薪或時薪
    start_date: date
    end_date: date | None  # None = 還沒結束


@dataclass(frozen=True)
class PayslipLine:
    """對應 payslip_lines 表的一列。一份合約在這個月算一行。"""

    contract_id: int
    salary_type: SalaryType
    salary_amount: Decimal  # 快照：計算當下的月薪或時薪
    start_date: date  # 合約和這個月的交集
    end_date: date
    amount: Decimal  # 已經進位到整數元
    paid_days: int | None = None  # 月薪才有
    divisor_days: int | None = None  # 月薪才有
    work_hours: Decimal | None = None  # 時薪才有


@dataclass(frozen=True)
class Payslip:
    """對應 payslips 表的一列，加上它的明細。"""

    lines: list[PayslipLine]
    amount: Decimal  # = 明細加總
    needs_review: bool  # R4


def round_amount(x: Decimal) -> Decimal:
    """R5：ROUND_HALF_UP 進位到整數元。

    例：Decimal("1372.5") → Decimal("1373")
    """
    return x.to_integral_value(rounding=ROUND_HALF_UP)


def month_range(year: int, month: int) -> tuple[date, date]:
    """這個月的第一天和最後一天。"""
    if month == 12:
        next_month_first_day = date(year + 1, 1, 1)
    else:
        next_month_first_day = date(year, month + 1, 1)
    return date(year, month, 1), next_month_first_day - timedelta(days=1)


def month_overlap(
    contract: Contract, year: int, month: int
) -> tuple[date, date] | None:
    """合約期間和這個月的交集（頭尾都算）。沒有交集回傳 None。

    例：合約 2026-03-10 ～ None，2026 年 3 月 → (date(2026, 3, 10), date(2026, 3, 31))
    例：合約 2026-01-01 ～ 2026-02-15，2026 年 3 月 → None
    """
    first_day, last_day = month_range(year, month)

    start = max(contract.start_date, first_day)
    end = min(contract.end_date or last_day, last_day)

    if start > end:
        return None
    return start, end


def monthly_line(contract: Contract, year: int, month: int) -> PayslipLine:
    """R1：一份月薪合約在這個月的明細。呼叫前已確定合約和這個月有交集。

    例：月薪 50,000，2026-03-10 到職，2026 年 3 月 →
        PayslipLine(start_date=3/10, end_date=3/31, paid_days=22, divisor_days=30,
                    amount=Decimal("36667"), ...)
    例：月薪 50,000，整個 2 月都有效 → paid_days=30, amount=Decimal("50000")
    """
    overlap = month_overlap(contract, year, month)
    if overlap is None:
        raise ValueError("無交集，外層接")
    start, end = overlap

    divisor_days = DIVISOR_DAYS
    if (start, end) == month_range(year, month):
        paid_days = divisor_days  # 整個月都有效 → 視為 30 天，不管這個月實際幾天
    else:
        paid_days = (end - start).days + 1
    amount = round_amount(
        contract.salary_amount * Decimal(paid_days) / Decimal(divisor_days)
    )

    return PayslipLine(
        contract_id=contract.contract_id,
        salary_type=contract.salary_type,
        salary_amount=contract.salary_amount,
        start_date=start,
        end_date=end,
        amount=amount,
        paid_days=paid_days,
        divisor_days=divisor_days,
    )


def hourly_line(
    contract: Contract, year: int, month: int, hours: Decimal
) -> PayslipLine:
    """R3、R4：一份時薪合約在這個月的明細。hours 是這個月的工時加總，沒有工時就是 0。

    例：時薪 200，hours=Decimal("37.5") → work_hours=37.5, amount=Decimal("7500")
    例：hours=Decimal("0") → work_hours=0, amount=Decimal("0")
    """
    overlap = month_overlap(contract, year, month)
    if overlap is None:
        raise ValueError("無交集，外層接")
    start, end = overlap

    amount = round_amount(contract.salary_amount * hours)

    return PayslipLine(
        contract_id=contract.contract_id,
        salary_type=contract.salary_type,
        salary_amount=contract.salary_amount,
        start_date=start,
        end_date=end,
        amount=amount,
        work_hours=hours,
    )


def calculate_payslip(
    year: int,
    month: int,
    contracts: list[Contract],
    hours_by_contract: dict[int, Decimal],
) -> Payslip | None:
    """R2、R4、R7：某個人在這個月的薪資單。

    contracts：這個人的合約（可能有跟這個月沒交集的，要自己濾掉）
    hours_by_contract：{contract_id: 這個月的工時加總}，沒有工時的合約不會出現在裡面

    - 每份有交集的合約算一行（月薪 → monthly_line，時薪 → hourly_line）
    - 有任何時薪合約工時是 0 → needs_review=True
    - 完全沒有合約跟這個月有交集 → 回傳 None（R7，worker 看到就刪掉薪資單）
    """
    lines = []
    for contract in contracts:
        if month_overlap(contract, year, month) is None:
            continue
        if contract.salary_type == "monthly":
            lines.append(monthly_line(contract, year, month))
        else:
            hours = hours_by_contract.get(contract.contract_id, Decimal(0))
            lines.append(hourly_line(contract, year, month, hours))

    if not lines:
        return None

    return Payslip(
        lines=lines,
        amount=sum((line.amount for line in lines), Decimal(0)),
        needs_review=any(
            line.salary_type == "hourly" and line.work_hours == 0 for line in lines
        ),
    )
