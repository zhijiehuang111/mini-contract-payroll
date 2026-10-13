"""計算的測試，對應 SPEC 第 9 節「計算」。"""

from datetime import date
from decimal import Decimal

import pytest

from payroll.calc import (
    Contract,
    calculate_payslip,
    hourly_line,
    monthly_line,
    round_amount,
)


def monthly(
    start: date, end: date | None = None, amount: int = 50000, contract_id: int = 1
) -> Contract:
    return Contract(contract_id, "monthly", Decimal(amount), start, end)


def hourly(rate: int, contract_id: int = 1) -> Contract:
    return Contract(contract_id, "hourly", Decimal(rate), date(2026, 1, 1), None)


# R1：月薪日割


@pytest.mark.parametrize(
    ("year", "month"),
    [(2026, 3), (2026, 4), (2026, 2), (2028, 2)],  # 31 天、30 天、2 月、閏年 2 月
)
def test_monthly_full_month(year, month):
    line = monthly_line(monthly(date(2025, 1, 1)), year, month)

    assert line.paid_days == 30
    assert line.amount == Decimal(50000)


def test_monthly_mid_month_start():
    line = monthly_line(monthly(date(2026, 3, 10)), 2026, 3)

    assert line.paid_days == 22
    assert line.divisor_days == 30
    assert line.amount == Decimal(36667)


def test_monthly_mid_month_end():
    line = monthly_line(monthly(date(2025, 1, 1), date(2026, 3, 10)), 2026, 3)

    assert line.paid_days == 10
    assert line.amount == Decimal(16667)


def test_monthly_start_and_end_in_same_month():
    line = monthly_line(monthly(date(2026, 3, 5), date(2026, 3, 20)), 2026, 3)

    assert (line.start_date, line.end_date) == (date(2026, 3, 5), date(2026, 3, 20))
    assert line.paid_days == 16
    assert line.amount == Decimal(26667)


def test_monthly_one_day():
    line = monthly_line(monthly(date(2026, 3, 31)), 2026, 3)

    assert line.paid_days == 1
    assert line.amount == Decimal(1667)


def test_monthly_30_days_in_31_day_month_is_full_pay():
    line = monthly_line(monthly(date(2026, 3, 2)), 2026, 3)

    assert line.paid_days == 30
    assert line.amount == Decimal(50000)


def test_monthly_feb_2_start():
    line = monthly_line(monthly(date(2026, 2, 2)), 2026, 2)

    assert line.paid_days == 27
    assert line.amount == Decimal(45000)


# R2：同月離職又再到職


def test_rehire_in_same_month():
    contracts = [
        monthly(date(2026, 3, 1), date(2026, 3, 10), 50000, contract_id=1),
        monthly(date(2026, 3, 20), None, 55000, contract_id=2),
    ]

    payslip = calculate_payslip(2026, 3, contracts, {})
    assert payslip is not None

    assert [line.amount for line in payslip.lines] == [Decimal(16667), Decimal(22000)]
    assert payslip.amount == Decimal(38667)


# R3：時薪


def test_hourly_decimal_hours():
    line = hourly_line(hourly(200), 2026, 3, Decimal("37.5"))

    assert line.work_hours == Decimal("37.5")
    assert line.amount == Decimal(7500)


# R4：缺工時


def test_hourly_without_hours_needs_review():
    payslip = calculate_payslip(2026, 3, [hourly(200)], {})
    assert payslip is not None

    assert payslip.amount == Decimal(0)
    assert payslip.lines[0].work_hours == Decimal(0)
    assert payslip.needs_review is True


def test_hourly_with_hours_no_review():
    payslip = calculate_payslip(2026, 3, [hourly(200)], {1: Decimal(10)})
    assert payslip is not None

    assert payslip.needs_review is False


# R5：進位


def test_round_half_up():
    assert round_amount(Decimal("1372.5")) == Decimal(1373)  # round() 會得到 1372
    assert round_amount(Decimal("1372.4")) == Decimal(1372)


def test_hourly_rounds_half_up():
    line = hourly_line(hourly(183), 2026, 3, Decimal("7.5"))

    assert line.amount == Decimal(1373)


def test_multiply_before_divide():
    # 先算日薪再進位會得到 1667 × 22 = 36674
    line = monthly_line(monthly(date(2026, 3, 10)), 2026, 3)

    assert line.amount == Decimal(36667)


# R7：範圍


def test_no_contract_in_month_returns_none():
    contracts = [
        monthly(date(2026, 1, 1), date(2026, 2, 15), contract_id=1),
        monthly(date(2026, 4, 1), contract_id=2),
    ]

    assert calculate_payslip(2026, 3, contracts, {}) is None
