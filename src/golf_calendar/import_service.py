"""The service layer: the only entrypoint to the importer's behaviour.

Depends on the :class:`CalendarGateway` port rather than on Google, so every decision here
is unit-testable against a fake.
"""

from __future__ import annotations

from dataclasses import dataclass

from golf_calendar.domain import GolfCalendarError
from golf_calendar.google_calendar_gateway import CalendarGateway
from golf_calendar.google_calendar_gateway import CalendarRef


class AmbiguousCalendarError(GolfCalendarError):
    """Raised when more than one writable calendar carries the configured name."""

    code = "calendar_ambiguous"


class ReadOnlyCalendarError(GolfCalendarError):
    """Raised when the only calendar of that name cannot be written to."""

    code = "calendar_read_only"


@dataclass(frozen=True, slots=True)
class EnsuredCalendar:
    """The calendar the import will write to, and whether this run had to create it."""

    calendar: CalendarRef
    created: bool


def ensure_calendar(gateway: CalendarGateway, name: str, timezone: str) -> EnsuredCalendar:
    """Return the calendar called ``name``, creating it only if it does not exist.

    Matching is exact. A prefix or case-insensitive match could silently hijack an
    unrelated calendar, and picking one of two identically-named calendars would scatter a
    season into the wrong place — so ambiguity is an error rather than a guess.

    Shared and subscribed calendars come back in the same list, so a read-only one of the
    right name is reported rather than adopted; otherwise every event of the import would
    fail against it, long after this check claimed success.
    """
    named = [calendar for calendar in gateway.list_calendars() if calendar.summary == name]
    matches = [calendar for calendar in named if calendar.is_writable]
    if not matches and named:
        roles = ", ".join(sorted({calendar.access_role or "unknown" for calendar in named}))
        raise ReadOnlyCalendarError(
            f"a calendar named {name!r} exists but cannot be written to (access: {roles}) — "
            "set CALENDAR_NAME to a name you own"
        )
    if len(matches) > 1:
        listed = ", ".join(sorted(calendar.calendar_id for calendar in matches))
        raise AmbiguousCalendarError(
            f"{len(matches)} writable calendars are named {name!r}: {listed} — "
            "rename or remove the duplicates, or set CALENDAR_NAME to something unique"
        )
    if matches:
        return EnsuredCalendar(calendar=matches[0], created=False)
    return EnsuredCalendar(calendar=gateway.create_calendar(name, timezone), created=True)
