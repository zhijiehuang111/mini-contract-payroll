"""計算的測試，對應 SPEC 第 9 節「計算」。"""

from datetime import date
from decimal import Decimal

from payroll.calc import Contract, monthly_line


# 範例：照這個格式寫其他測試。數字直接用 SPEC 第 4 節的例子
def test_monthly_mid_month_start():
    contract = Contract(
        contract_id=1,
        salary_type="monthly",
        salary_amount=Decimal(50000),
        start_date=date(2026, 3, 10),
        end_date=None,
    )

    line = monthly_line(contract, 2026, 3)

    assert line.paid_days == 22
    assert line.divisor_days == 30
    assert line.amount == Decimal(36667)
