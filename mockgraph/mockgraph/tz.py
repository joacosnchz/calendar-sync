"""Time zone names as Graph sends them: IANA or Windows."""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal.windows_tz import win_tz


@lru_cache(maxsize=512)
def resolve_tz(name: str | None) -> ZoneInfo | None:
    """Resolve an IANA or Windows time zone name (Graph returns either)."""
    if not name:
        return None
    for candidate in (name, win_tz.get(name)):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return None
