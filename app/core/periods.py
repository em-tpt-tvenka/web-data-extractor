from __future__ import annotations

from datetime import date, datetime, timezone

from app.core.config import settings

def _today() -> date:
    return datetime.now(timezone.utc).date()

def _subtract_months(d: date, months: int) -> date:
    month_index = (d.year * 12 + (d.month - 1)) - months
    year, month = divmod(month_index, 12)
    month += 1
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last_day = (next_month_first.toordinal() - 1)
    day = min(d.day, date.fromordinal(last_day).day)
    return date(year, month, day)

_RELATIVE_MONTHS = {
    "monthly": 1,
    "quarterly": 3,
    "last_1_year": 12,
    "last_2_years": 24,
}

def resolve_window(date_range) -> tuple[date, date]:
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
    start, end = resolve_window(date_range)
    if end < start:
        start, end = end, start

    prefix = settings.report_scope_prefix
    labels: list[str] = []
    seen: set[str] = set()

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
    if d is None:
        return True
    start, end = window
    return start <= d <= end
