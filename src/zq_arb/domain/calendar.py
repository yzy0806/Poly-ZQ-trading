from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class MeetingCalendar:
    """Calendar-day exposure to one explicitly configured rate effective date."""

    contract_month: str
    rate_effective_date: date

    def __post_init__(self) -> None:
        if not re.fullmatch(r"\d{6}", self.contract_month):
            raise ValueError("contract month must use YYYYMM")
        year, month = int(self.contract_month[:4]), int(self.contract_month[4:])
        date(year, month, 1)
        if self.rate_effective_date.strftime("%Y%m") != self.contract_month:
            raise ValueError("rate effective date must be in the target contract month")
        if self.days_before <= 0:
            raise ValueError("meeting calendar requires pre-decision and post-decision days")

    @property
    def days_before(self) -> int:
        return self.rate_effective_date.day - 1

    @property
    def total_days(self) -> int:
        return calendar.monthrange(int(self.contract_month[:4]), int(self.contract_month[4:]))[1]

    @property
    def days_after(self) -> int:
        return self.total_days - self.days_before

    @property
    def post_decision_weight(self) -> Decimal:
        return Decimal(self.days_after) / Decimal(self.total_days)

    @property
    def symbol(self) -> str:
        return f"ZQ{'FGHJKMNQUVXZ'[int(self.contract_month[4:]) - 1]}{self.contract_month[3]}"


def next_contract_month(month: str) -> str:
    year, number = int(month[:4]), int(month[4:])
    return f"{year + (number == 12):04d}{number % 12 + 1:02d}"
