"""Tests for the import service, driven through a fake calendar gateway."""

from __future__ import annotations

import pytest

from golf_calendar.google_calendar_gateway import CalendarRef
from golf_calendar.import_service import AmbiguousCalendarError
from golf_calendar.import_service import EnsuredCalendar
from golf_calendar.import_service import ReadOnlyCalendarError
from golf_calendar.import_service import ensure_calendar

CALENDAR_NAME = "Golf training"
TIMEZONE = "Europe/Madrid"


def calendar(calendar_id: str, summary: str, access_role: str = "owner") -> CalendarRef:
    """A calendar the account owns, unless the test says otherwise."""
    return CalendarRef(calendar_id=calendar_id, summary=summary, access_role=access_role)


class FakeCalendarGateway:
    """An in-memory calendar account that records what was asked of it."""

    def __init__(self, calendars: list[CalendarRef] | None = None) -> None:
        self.calendars = list(calendars or [])
        self.list_calls = 0
        self.created: list[tuple[str, str]] = []

    def list_calendars(self) -> tuple[CalendarRef, ...]:
        self.list_calls += 1
        return tuple(self.calendars)

    def create_calendar(self, name: str, timezone: str) -> CalendarRef:
        self.created.append((name, timezone))
        created = calendar(f"created-{len(self.created)}", name)
        self.calendars.append(created)
        return created


class TestEnsureCalendar:
    def test_reuses_an_existing_calendar_without_creating_one(self) -> None:
        existing = calendar("cal-1", CALENDAR_NAME)
        gateway = FakeCalendarGateway([existing])

        ensured = ensure_calendar(gateway, CALENDAR_NAME, TIMEZONE)

        assert ensured == EnsuredCalendar(calendar=existing, created=False)
        assert gateway.created == []

    def test_creates_the_calendar_when_the_account_has_none(self) -> None:
        gateway = FakeCalendarGateway()

        ensured = ensure_calendar(gateway, CALENDAR_NAME, TIMEZONE)

        assert ensured == EnsuredCalendar(
            calendar=calendar("created-1", CALENDAR_NAME), created=True
        )
        assert gateway.created == [(CALENDAR_NAME, TIMEZONE)]

    def test_a_second_run_reuses_what_the_first_created(self) -> None:
        gateway = FakeCalendarGateway()

        first = ensure_calendar(gateway, CALENDAR_NAME, TIMEZONE)
        second = ensure_calendar(gateway, CALENDAR_NAME, TIMEZONE)

        assert first.calendar == second.calendar
        assert second.created is False
        assert len(gateway.created) == 1

    @pytest.mark.parametrize(
        "other_name",
        ["golf training", "Golf Training", "Golf training 2025", " Golf training", "Golf"],
    )
    def test_does_not_reuse_a_similarly_named_calendar(self, other_name: str) -> None:
        """A loose match would silently write a whole season into someone else's calendar."""
        gateway = FakeCalendarGateway([calendar("other", other_name)])

        ensured = ensure_calendar(gateway, CALENDAR_NAME, TIMEZONE)

        assert ensured.created is True
        assert ensured.calendar.calendar_id == "created-1"

    def test_picks_the_right_calendar_from_a_crowded_account(self) -> None:
        wanted = calendar("cal-2", CALENDAR_NAME)
        gateway = FakeCalendarGateway(
            [
                calendar("cal-1", "Personal"),
                wanted,
                calendar("cal-3", "Trabajo"),
            ]
        )

        ensured = ensure_calendar(gateway, CALENDAR_NAME, TIMEZONE)

        assert ensured == EnsuredCalendar(calendar=wanted, created=False)

    def test_refuses_to_guess_between_two_calendars_of_the_same_name(self) -> None:
        gateway = FakeCalendarGateway(
            [
                calendar("cal-b", CALENDAR_NAME),
                calendar("cal-a", CALENDAR_NAME),
            ]
        )

        with pytest.raises(AmbiguousCalendarError, match="2 writable calendars are named"):
            ensure_calendar(gateway, CALENDAR_NAME, TIMEZONE)

    def test_names_the_duplicate_calendars_so_they_can_be_found(self) -> None:
        gateway = FakeCalendarGateway(
            [
                calendar("cal-b", CALENDAR_NAME),
                calendar("cal-a", CALENDAR_NAME),
            ]
        )

        with pytest.raises(AmbiguousCalendarError, match="cal-a, cal-b"):
            ensure_calendar(gateway, CALENDAR_NAME, TIMEZONE)

    def test_creates_nothing_when_the_name_is_ambiguous(self) -> None:
        gateway = FakeCalendarGateway(
            [
                calendar("cal-a", CALENDAR_NAME),
                calendar("cal-b", CALENDAR_NAME),
            ]
        )

        with pytest.raises(AmbiguousCalendarError):
            ensure_calendar(gateway, CALENDAR_NAME, TIMEZONE)

        assert gateway.created == []

    def test_refuses_a_calendar_it_cannot_write_to(self) -> None:
        """Adopting a shared read-only calendar would fail once per event, far too late."""
        gateway = FakeCalendarGateway([calendar("shared", CALENDAR_NAME, access_role="reader")])

        with pytest.raises(ReadOnlyCalendarError, match="cannot be written to"):
            ensure_calendar(gateway, CALENDAR_NAME, TIMEZONE)

    def test_creates_nothing_when_the_only_match_is_read_only(self) -> None:
        gateway = FakeCalendarGateway([calendar("shared", CALENDAR_NAME, access_role="reader")])

        with pytest.raises(ReadOnlyCalendarError):
            ensure_calendar(gateway, CALENDAR_NAME, TIMEZONE)

        assert gateway.created == []

    @pytest.mark.parametrize("role", ["owner", "writer"])
    def test_reuses_a_calendar_it_can_write_to(self, role: str) -> None:
        existing = calendar("cal-1", CALENDAR_NAME, access_role=role)
        gateway = FakeCalendarGateway([existing])

        ensured = ensure_calendar(gateway, CALENDAR_NAME, TIMEZONE)

        assert ensured == EnsuredCalendar(calendar=existing, created=False)

    def test_prefers_the_writable_calendar_over_a_read_only_namesake(self) -> None:
        writable = calendar("mine", CALENDAR_NAME)
        gateway = FakeCalendarGateway(
            [calendar("shared", CALENDAR_NAME, access_role="reader"), writable]
        )

        ensured = ensure_calendar(gateway, CALENDAR_NAME, TIMEZONE)

        assert ensured == EnsuredCalendar(calendar=writable, created=False)

    def test_reads_the_calendar_list_only_once(self) -> None:
        """One round trip for the whole lookup, not one per candidate."""
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])

        ensure_calendar(gateway, CALENDAR_NAME, TIMEZONE)

        assert gateway.list_calls == 1
