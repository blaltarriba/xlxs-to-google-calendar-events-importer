"""The service layer: the only entrypoint to the importer's behaviour.

Depends on the :class:`CalendarGateway` port rather than on Google, so every decision here
is unit-testable against a fake.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from datetime import datetime

from golf_calendar.config import ImportSettings
from golf_calendar.domain import GolfCalendarError
from golf_calendar.domain import Season
from golf_calendar.domain import TrainingSession
from golf_calendar.domain import TrainingWeekday
from golf_calendar.google_calendar_gateway import CalendarEvent
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


def find_calendar(gateway: CalendarGateway, name: str) -> CalendarRef | None:
    """Return the writable calendar called ``name``, or ``None``. Creates nothing.

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
    return matches[0] if matches else None


def ensure_calendar(gateway: CalendarGateway, name: str, timezone: str) -> EnsuredCalendar:
    """Return the calendar called ``name``, creating it only if it does not exist."""
    existing = find_calendar(gateway, name)
    if existing is not None:
        return EnsuredCalendar(calendar=existing, created=False)
    return EnsuredCalendar(calendar=gateway.create_calendar(name, timezone), created=True)


class InvalidLimitError(GolfCalendarError):
    """Raised when the requested number of events to create makes no sense."""

    code = "invalid_limit"


@dataclass(frozen=True, slots=True)
class ImportPlan:
    """What an import would do, worked out without writing anything."""

    calendar_name: str
    calendar: CalendarRef | None
    to_create: tuple[TrainingSession, ...]
    already_present: tuple[TrainingSession, ...]
    deferred: tuple[TrainingSession, ...]

    @property
    def needs_a_new_calendar(self) -> bool:
        """Whether running the import for real would have to create the calendar."""
        return self.calendar is None


@dataclass(frozen=True, slots=True)
class ImportReport:
    """What an import did."""

    plan: ImportPlan
    calendar: CalendarRef | None
    calendar_was_created: bool
    created_event_ids: tuple[str, ...]


class PartialImportError(GolfCalendarError):
    """Raised when the run failed part-way, naming how far it got."""

    code = "import_incomplete"


def import_key(season: Season, weekday: TrainingWeekday) -> str:
    """The marker stamped on every event this tool creates, and matched on a re-run.

    Scoped to the season and weekday, so importing a second weekday into the same calendar
    later cannot make the two runs mistake each other's events for their own.
    """
    return f"golf-training-{season.key}-{weekday.key}"


def plan_import(
    settings: ImportSettings,
    gateway: CalendarGateway,
    season: Season,
    limit: int | None = None,
) -> ImportPlan:
    """Work out which sessions are missing. Writes nothing, and creates no calendar.

    This is what ``--dry-run`` reports, so it must stay free of side effects. Existence is
    answered by a single filtered query rather than one lookup per session.
    """
    if limit is not None and limit < 1:
        raise InvalidLimitError(f"--limit must be at least 1, got {limit}")
    name = settings.schedule.calendar_name
    calendar = find_calendar(gateway, name)
    if not season.sessions:
        return ImportPlan(
            calendar_name=name,
            calendar=calendar,
            to_create=(),
            already_present=(),
            deferred=(),
        )

    imported = _already_imported(settings, gateway, season, calendar)
    missing = tuple(s for s in season.sessions if s.session_date not in imported)
    present = tuple(s for s in season.sessions if s.session_date in imported)
    to_create = missing if limit is None else missing[:limit]
    return ImportPlan(
        calendar_name=name,
        calendar=calendar,
        to_create=to_create,
        already_present=present,
        deferred=missing[len(to_create) :],
    )


def import_training_sessions(
    settings: ImportSettings,
    gateway: CalendarGateway,
    season: Season,
    limit: int | None = None,
) -> ImportReport:
    """Create every session the calendar is missing, up to ``limit``.

    Safe to re-run and safe to interrupt: each event is stamped with its session date, so a
    later run recognises what is already there and creates only the rest.
    """
    plan = plan_import(settings, gateway, season, limit)
    if not plan.to_create:
        # Nothing to write, so nothing to create a calendar for either.
        return ImportReport(
            plan=plan,
            calendar=plan.calendar,
            calendar_was_created=False,
            created_event_ids=(),
        )
    calendar = plan.calendar or gateway.create_calendar(
        plan.calendar_name, settings.schedule.timezone
    )
    created = _create_events(settings, gateway, season, plan, calendar)
    return ImportReport(
        plan=plan,
        calendar=calendar,
        calendar_was_created=plan.needs_a_new_calendar,
        created_event_ids=created,
    )


def build_event(session: TrainingSession, settings: ImportSettings, key: str) -> CalendarEvent:
    """Render one session into the event to create."""
    schedule = settings.schedule
    filled = {
        "session": session.number,
        "total": session.total,
        "date": session.session_date.isoformat(),
        "location": schedule.event_location,
    }
    return CalendarEvent(
        summary=schedule.event_title_template.format(**filled),
        description=schedule.event_description_template.format(**filled),
        location=schedule.event_location,
        starts_at=datetime.combine(session.session_date, schedule.start_time),
        ends_at=datetime.combine(session.session_date, schedule.end_time),
        timezone=schedule.timezone,
        invitees=(settings.invitee_email,),
        # Training must not make the calendar owner look busy.
        shows_as_busy=False,
        import_key=key,
        session_number=session.number,
    )


def _create_events(
    settings: ImportSettings,
    gateway: CalendarGateway,
    season: Season,
    plan: ImportPlan,
    calendar: CalendarRef,
) -> tuple[str, ...]:
    """Create each planned event, reporting how far it got if one fails.

    Re-running is idempotent, so a partial run is recoverable — but only if the user is
    told that some events already exist.
    """
    key = import_key(season, settings.schedule.training_weekday)
    created: list[str] = []
    for session in plan.to_create:
        try:
            created.append(
                gateway.create_event(calendar.calendar_id, build_event(session, settings, key))
            )
        except GolfCalendarError as error:
            raise PartialImportError(
                f"created {len(created)} of {len(plan.to_create)} events before failing on "
                f"session {session.number} ({session.session_date.isoformat()}): {error} — "
                "re-run to continue from where it stopped"
            ) from error
    return tuple(created)


def _already_imported(
    settings: ImportSettings,
    gateway: CalendarGateway,
    season: Season,
    calendar: CalendarRef | None,
) -> frozenset[date]:
    """Which session dates the calendar already holds.

    A calendar that does not exist yet holds nothing, so that round trip is skipped.
    """
    if calendar is None:
        return frozenset()
    return gateway.list_imported_dates(
        calendar.calendar_id, import_key(season, settings.schedule.training_weekday)
    )
