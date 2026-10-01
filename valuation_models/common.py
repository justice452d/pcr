from __future__ import annotations

from datetime import date


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def calculate_percentile_rank(values, current) -> float | None:
    """Percentage of finite history observations less than or equal to current."""
    if current is None:
        return None
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return None
    return 100.0 * sum(value <= float(current) for value in clean) / len(clean)


def window_start(day: str, years: int | None) -> date | None:
    if years is None:
        return None
    current = date.fromisoformat(day)
    try:
        return current.replace(year=current.year - years)
    except ValueError:
        return current.replace(year=current.year - years, day=28)


