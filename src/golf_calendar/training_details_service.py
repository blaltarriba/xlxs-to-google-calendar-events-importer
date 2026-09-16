"""Adds each session's training detail to the events a previous import created.

Depends on the :class:`CalendarGateway` port rather than on Google, so every decision here
is unit-testable against a fake.

The new title and description are rendered afresh from the configured templates plus the
detail, then compared with what the calendar holds, so only events that differ are planned.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from golf_calendar.config import ImportSettings
from golf_calendar.domain import GolfCalendarError
from golf_calendar.domain import Season
from golf_calendar.domain import TrainingDetail
from golf_calendar.domain import TrainingSession
from golf_calendar.google_calendar_gateway import CalendarGateway
from golf_calendar.google_calendar_gateway import CalendarRef
from golf_calendar.google_calendar_gateway import ImportedEvent
from golf_calendar.import_service import build_event
from golf_calendar.import_service import find_calendar
from golf_calendar.import_service import import_key


class CalendarNotFoundError(GolfCalendarError):
    """Raised when there is no calendar whose events could be given details."""

    code = "calendar_not_found"


@dataclass(frozen=True, slots=True)
class DetailUpdate:
    """The text one event should carry once its session's detail is added."""

    session: TrainingSession
    detail: TrainingDetail
    event_id: str
    summary: str
    description: str


@dataclass(frozen=True, slots=True)
class TrainingDetailsPlan:
    """Which events need their text changed, worked out without writing anything."""

    calendar: CalendarRef
    to_update: tuple[DetailUpdate, ...]
    already_current: tuple[TrainingSession, ...]
    without_event: tuple[TrainingSession, ...]


def plan_training_details(
    settings: ImportSettings,
    gateway: CalendarGateway,
    season: Season,
    details: Sequence[TrainingDetail],
) -> TrainingDetailsPlan:
    """Work out which events need their detail added or corrected. Writes nothing.

    Details for dates the season does not train on are left out.
    """
    sessions = _sessions_by_date(season)
    calendar = _existing_calendar(gateway, settings.schedule.calendar_name)
    key = import_key(season, settings.schedule.training_weekday)
    events = _imported_events_by_date(gateway, calendar, key)
    matched = [
        (detail, sessions[detail.session_date])
        for detail in details
        if detail.session_date in sessions
    ]
    updates = [
        (event, _build_update(session, detail, event, settings, key))
        for detail, session in matched
        if (event := events.get(detail.session_date)) is not None
    ]
    return TrainingDetailsPlan(
        calendar=calendar,
        to_update=tuple(update for event, update in updates if not _is_current(event, update)),
        already_current=tuple(
            update.session for event, update in updates if _is_current(event, update)
        ),
        without_event=tuple(
            session for detail, session in matched if detail.session_date not in events
        ),
    )


def _sessions_by_date(season: Season) -> dict[date, TrainingSession]:
    return {session.session_date: session for session in season.sessions}


def _existing_calendar(gateway: CalendarGateway, name: str) -> CalendarRef:
    calendar = find_calendar(gateway, name)
    if calendar is None:
        raise CalendarNotFoundError(
            f"no calendar named {name!r} — run `import` first to create its events"
        )
    return calendar


def _imported_events_by_date(
    gateway: CalendarGateway, calendar: CalendarRef, key: str
) -> dict[date, ImportedEvent]:
    """One query for every event the import created, indexed by the session it belongs to."""
    events = gateway.list_imported_events(calendar.calendar_id, key)
    return {event.session_date: event for event in events}


def _build_update(
    session: TrainingSession,
    detail: TrainingDetail,
    event: ImportedEvent,
    settings: ImportSettings,
    key: str,
) -> DetailUpdate:
    base = build_event(session, settings, key)
    return DetailUpdate(
        session=session,
        detail=detail,
        event_id=event.event_id,
        summary=f"{base.summary} · {detail.activities}",
        description=f"{base.description}\n\n{detail.group_label}: {detail.activities}",
    )


def _is_current(event: ImportedEvent, update: DetailUpdate) -> bool:
    return event.summary == update.summary and event.description == update.description
