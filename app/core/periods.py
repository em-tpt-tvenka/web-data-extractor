"""Scraping-period helpers.

Turns a request ``date_range`` into a concrete [start, end] date window, and
derives calendar-quarter report-scope ids (e.g. ``TND_Q2_FY27``) that cover that
window. Quarters use the calendar convention: Q1=Jan-Mar, Q2=Apr-Jun,
Q3=Jul-Sep, Q4=Oct-Dec; the fiscal-year label is the calendar year (2 digits).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.core.config import settings


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _subtract_months(d: date, months: int) -> date:
    """Shift a date back by a number of months, clamping the day of month."""
    month_index = (d.year * 12 + (d.month - 1)) - months
    year, month = divmod(month_index, 12)
    month += 1
    # Clamp day to the last valid day of the target month.
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last_day = (next_month_first.toordinal() - 1)
    day = min(d.day, date.fromordinal(last_day).day)
    return date(year, month, day)


# Relative range types -> number of months to look back.
_RELATIVE_MONTHS = {
    "monthly": 1,
    "quarterly": 3,
    "last_1_year": 12,
    "last_2_years": 24,
}


def resolve_window(date_range) -> tuple[date, date]:
    """Return the (start_date, end_date) window for a DateRange.

    For custom ranges the explicit dates are used (falling back to a wide-open
    window if one side is missing). For relative ranges the window ends today and
    starts the configured number of months earlier.
    """
    rtype = getattr(date_range, "type", None)
    rtype = rtype.value if hasattr(rtype, "value") else rtype
    today = _today()

    if rtype == "custom":
        start = getattr(date_range, "start_date", None)
        end = getattr(date_range, "end_date", None)
        return (start or date(1970, 1, 1), end or today)

    months = _RELATIVE_MONTHS.get(rtype, 12)
    return (_subtract_months(today, months), today)


def _quarter(month: int) -> int:
    return (month - 1) // 3 + 1


def report_scope_ids(date_range) -> list[str]:
    """Derive the list of calendar-quarter scope ids covering the window, e.g.
    ``["TND_Q1_FY27", "TND_Q2_FY27"]``. Ordered, de-duplicated."""
    start, end = resolve_window(date_range)
    if end < start:
        start, end = end, start

    prefix = settings.report_scope_prefix
    labels: list[str] = []
    seen: set[str] = set()

    # Walk quarter by quarter from the quarter containing `start`.
    year = start.year
    quarter = _quarter(start.month)
    while (year, quarter) <= (end.year, _quarter(end.month)):
        label = f"{prefix}_Q{quarter}_FY{year % 100:02d}"
        if label not in seen:
            seen.add(label)
            labels.append(label)
        quarter += 1
        if quarter > 4:
            quarter = 1
            year += 1
    return labels


def in_window(d: date | None, window: tuple[date, date]) -> bool:
    """Whether a date falls within the window. Unknown dates (None) are kept, so
    records without a parseable date are never silently dropped."""
    if d is None:
        return True
    start, end = window
    return start <= d <= end
