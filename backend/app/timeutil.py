from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def isoformat_z(dt: datetime) -> str:
    """Format for Graph query parameters and subscription expiry (UTC, 'Z' suffix)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sync_window(now: datetime, past_days: int, future_days: int) -> tuple[datetime, datetime]:
    start = (now - timedelta(days=past_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (now + timedelta(days=future_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end
