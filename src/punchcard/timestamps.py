"""Timezone-aware UTC storage and local editing conversions."""
from datetime import datetime, timezone

UTC = timezone.utc


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds')


def local_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone()


def edited_local_timestamp(display_value: str, original: str) -> str:
    """Preserve precise seconds when a minute-resolution edit field was untouched.

    A note-only edit must not silently snap the session start/end to :00.
    If the user actually selects another minute, use that minute exactly.
    """
    original_local = local_datetime(original)
    if display_value == original_local.strftime('%Y-%m-%dT%H:%M'):
        return original
    candidate = datetime.fromisoformat(display_value)
    if candidate.tzinfo is None:
        candidate = candidate.astimezone()
    return candidate.isoformat()


