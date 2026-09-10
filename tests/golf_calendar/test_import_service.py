"""Tests for the import service, driven through a fake calendar gateway."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from datetime import datetime
from datetime import time
from pathlib import Path

import pytest

from golf_calendar.config import ImportSettings
from golf_calendar.config import ScheduleSettings
from golf_calendar.domain import Season
from golf_calendar.domain import TrainingSession
from golf_calendar.domain import TrainingWeekday
from golf_calendar.domain import number_sessions
from golf_calendar.google_calendar_gateway import CalendarError
from golf_calendar.google_calendar_gateway import CalendarEvent
from golf_calendar.google_calendar_gateway import CalendarRef
from golf_calendar.import_service import AmbiguousCalendarError
from golf_calendar.import_service import EnsuredCalendar
from golf_calendar.import_service import InvalidLimitError
from golf_calendar.import_service import PartialImportError
from golf_calendar.import_service import ReadOnlyCalendarError
from golf_calendar.import_service import build_event
from golf_calendar.import_service import ensure_calendar
from golf_calendar.import_service import import_key
from golf_calendar.import_service import import_training_sessions
from golf_calendar.import_service import plan_import

CALENDAR_NAME = "Golf training"
TIMEZONE = "Europe/Madrid"


def calendar(calendar_id: str, summary: str, access_role: str = "owner") -> CalendarRef:
    """A calendar the account owns, unless the test says otherwise."""
    return CalendarRef(calendar_id=calendar_id, summary=summary, access_role=access_role)


def build_settings(**overrides: object) -> ImportSettings:
    """Settings for an import, stating only what a test cares about."""
    schedule = ScheduleSettings(
        schedule_file=Path("schedule.xlsx"),
        training_weekday=TrainingWeekday.TUESDAY,
        extra_session_dates=(),
        calendar_name=CALENDAR_NAME,
        event_title_template="Entrenamiento {session}/{total}",
        event_description_template="Entrenamiento\nSesión {session} de {total}",
        event_location="Test location, Test City",
        start_time=time(17, 30),
        end_time=time(19, 30),
        timezone=TIMEZONE,
    )
    return ImportSettings(
        schedule=replace(schedule, **overrides),  # type: ignore[arg-type]
        invitee_email="wife@example.com",
        client_secret_file=Path("client_secret.json"),
        token_file=Path("token.json"),
    )


def build_season(*session_dates: date) -> Season:
    """A season of the given Tuesdays."""
    return Season(sessions=number_sessions(session_dates), declared_total=len(session_dates))


TUESDAYS = (date(2026, 9, 15), date(2026, 9, 22), date(2026, 9, 29), date(2026, 10, 6))


class FakeCalendarGateway:
    """An in-memory calendar account that records what was asked of it."""

    def __init__(self, calendars: list[CalendarRef] | None = None) -> None:
        self.calendars = list(calendars or [])
        self.list_calls = 0
        self.created: list[tuple[str, str]] = []
        self.events: list[CalendarEvent] = []
        self.queries: list[tuple[str, str]] = []

    def list_calendars(self) -> tuple[CalendarRef, ...]:
        self.list_calls += 1
        return tuple(self.calendars)

    def create_calendar(self, name: str, timezone: str) -> CalendarRef:
        self.created.append((name, timezone))
        created = calendar(f"created-{len(self.created)}", name)
        self.calendars.append(created)
        return created

    def list_imported_dates(self, calendar_id: str, import_key: str) -> frozenset[date]:
        self.queries.append((calendar_id, import_key))
        return frozenset(
            event.starts_at.date() for event in self.events if event.import_key == import_key
        )

    def create_event(self, calendar_id: str, event: CalendarEvent) -> str:
        self.events.append(event)
        return f"{calendar_id}-event-{len(self.events)}"


class FailingCalendarGateway(FakeCalendarGateway):
    """Fails when asked to create the nth event, to model an interrupted run."""

    def __init__(
        self, calendars: list[CalendarRef] | None = None, fail_on_event: int | None = None
    ) -> None:
        super().__init__(calendars)
        self.fail_on_event = fail_on_event

    def create_event(self, calendar_id: str, event: CalendarEvent) -> str:
        if self.fail_on_event is not None and len(self.events) + 1 == self.fail_on_event:
            raise CalendarError("Google Calendar rejected the request (429): Rate Limit")
        return super().create_event(calendar_id, event)


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


class TestPlanImport:
    def test_plans_every_session_for_an_empty_calendar(self) -> None:
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])

        plan = plan_import(build_settings(), gateway, build_season(*TUESDAYS))

        assert [session.number for session in plan.to_create] == [1, 2, 3, 4]
        assert plan.already_present == ()
        assert plan.deferred == ()

    def test_writes_nothing_while_planning(self) -> None:
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])

        plan_import(build_settings(), gateway, build_season(*TUESDAYS))

        assert gateway.events == []

    def test_a_limit_defers_the_rest(self) -> None:
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])

        plan = plan_import(build_settings(), gateway, build_season(*TUESDAYS), limit=2)

        assert [session.number for session in plan.to_create] == [1, 2]
        assert [session.number for session in plan.deferred] == [3, 4]

    @pytest.mark.parametrize("limit", [0, -1])
    def test_rejects_a_limit_below_one(self, limit: int) -> None:
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])

        with pytest.raises(InvalidLimitError, match="at least 1"):
            plan_import(build_settings(), gateway, build_season(*TUESDAYS), limit=limit)

    def test_a_limit_larger_than_the_season_is_harmless(self) -> None:
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])

        plan = plan_import(build_settings(), gateway, build_season(*TUESDAYS), limit=99)

        assert len(plan.to_create) == 4
        assert plan.deferred == ()

    def test_creates_no_calendar_while_planning(self) -> None:
        """--dry-run reports this plan, so planning must not touch the account."""
        gateway = FakeCalendarGateway()

        plan = plan_import(build_settings(), gateway, build_season(*TUESDAYS))

        assert gateway.created == []
        assert plan.calendar is None
        assert plan.needs_a_new_calendar is True

    def test_skips_the_existence_query_when_the_calendar_does_not_exist(self) -> None:
        """A calendar that does not exist holds nothing; asking would be a wasted round trip."""
        gateway = FakeCalendarGateway()

        plan = plan_import(build_settings(), gateway, build_season(*TUESDAYS))

        assert gateway.queries == []
        assert len(plan.to_create) == 4

    def test_asks_for_existing_events_only_once(self) -> None:
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])

        plan_import(build_settings(), gateway, build_season(*TUESDAYS))

        assert len(gateway.queries) == 1

    def test_the_query_is_scoped_to_this_season_and_weekday(self) -> None:
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])

        plan_import(build_settings(), gateway, build_season(*TUESDAYS))

        assert gateway.queries == [("cal-1", "golf-training-2026-tuesday")]

    def test_an_empty_season_plans_nothing(self) -> None:
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])

        plan = plan_import(build_settings(), gateway, Season(sessions=()))

        assert plan.to_create == ()
        assert gateway.queries == []


class TestImportTrainingSessions:
    def test_creates_the_whole_season_from_empty(self) -> None:
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])

        report = import_training_sessions(build_settings(), gateway, build_season(*TUESDAYS))

        assert len(report.created_event_ids) == 4
        assert [event.session_number for event in gateway.events] == [1, 2, 3, 4]

    def test_a_second_run_creates_nothing(self) -> None:
        """The whole point: re-running must be safe."""
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])
        season = build_season(*TUESDAYS)
        import_training_sessions(build_settings(), gateway, season)

        report = import_training_sessions(build_settings(), gateway, season)

        assert report.created_event_ids == ()
        assert [session.number for session in report.plan.already_present] == [1, 2, 3, 4]
        assert len(gateway.events) == 4

    def test_a_limited_run_then_an_unlimited_one_creates_each_session_once(self) -> None:
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])
        season = build_season(*TUESDAYS)

        first = import_training_sessions(build_settings(), gateway, season, limit=2)
        second = import_training_sessions(build_settings(), gateway, season)

        assert len(first.created_event_ids) == 2
        assert len(second.created_event_ids) == 2
        assert [event.session_number for event in gateway.events] == [1, 2, 3, 4]

    def test_fills_only_the_gaps_in_a_partly_populated_calendar(self) -> None:
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])
        season = build_season(*TUESDAYS)
        import_training_sessions(build_settings(), gateway, season, limit=1)
        gateway.events = [event for event in gateway.events if event.session_number == 1]

        report = import_training_sessions(build_settings(), gateway, season)

        assert [session.number for session in report.plan.to_create] == [2, 3, 4]
        assert [event.session_number for event in gateway.events] == [1, 2, 3, 4]

    def test_ignores_events_from_a_different_weekday_import(self) -> None:
        """Importing a second weekday must not make the two runs adopt each other's events."""
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])
        settings = build_settings()
        season = build_season(*TUESDAYS)
        import_training_sessions(settings, gateway, season)

        wednesday = build_settings(training_weekday=TrainingWeekday.WEDNESDAY)
        report = import_training_sessions(wednesday, gateway, season)

        assert len(report.created_event_ids) == 4
        assert len(gateway.events) == 8


class TestBuildEvent:
    def test_renders_the_whole_event(self) -> None:
        session = TrainingSession(session_date=date(2026, 12, 15), number=12, total=30)

        event = build_event(session, build_settings(), "golf-training-2026-2027-tuesday")

        assert event == CalendarEvent(
            summary="Entrenamiento 12/30",
            description="Entrenamiento\nSesión 12 de 30",
            location="Test location, Test City",
            # Naive on purpose: the timezone field carries the offset. See CalendarEvent.
            starts_at=datetime(2026, 12, 15, 17, 30),  # noqa: DTZ001
            ends_at=datetime(2026, 12, 15, 19, 30),  # noqa: DTZ001
            timezone="Europe/Madrid",
            invitees=("wife@example.com",),
            shows_as_busy=False,
            import_key="golf-training-2026-2027-tuesday",
            session_number=12,
        )

    def test_the_owner_stays_free_during_training(self) -> None:
        session = TrainingSession(session_date=date(2026, 12, 15), number=12, total=30)

        assert build_event(session, build_settings(), "key").shows_as_busy is False

    @pytest.mark.parametrize("session_date", [date(2026, 10, 20), date(2027, 4, 13)])
    def test_the_local_time_is_the_same_either_side_of_a_clock_change(
        self, session_date: date
    ) -> None:
        """Naive local time plus a timezone is what keeps 17:30 at 17:30 all season."""
        session = TrainingSession(session_date=session_date, number=1, total=1)

        event = build_event(session, build_settings(), "key")

        assert (event.starts_at.hour, event.starts_at.minute) == (17, 30)
        assert (event.ends_at.hour, event.ends_at.minute) == (19, 30)
        assert event.starts_at.tzinfo is None


class TestImportKey:
    def test_names_the_season_and_the_weekday(self) -> None:
        season = build_season(date(2026, 9, 15), date(2027, 6, 8))

        assert import_key(season, TrainingWeekday.TUESDAY) == "golf-training-2026-2027-tuesday"

    def test_differs_per_weekday(self) -> None:
        season = build_season(date(2026, 9, 15), date(2027, 6, 8))

        keys = {import_key(season, weekday) for weekday in TrainingWeekday}

        assert len(keys) == 3


class TestDryRunWritesNothing:
    def test_planning_creates_no_calendar_and_no_events(self) -> None:
        """--dry-run promises no writes, so plan_import must make none."""
        gateway = FakeCalendarGateway()

        plan_import(build_settings(), gateway, build_season(*TUESDAYS))

        assert gateway.created == []
        assert gateway.events == []

    def test_planning_reports_the_calendar_it_would_create(self) -> None:
        gateway = FakeCalendarGateway()

        plan = plan_import(build_settings(), gateway, build_season(*TUESDAYS))

        assert plan.calendar_name == CALENDAR_NAME
        assert plan.needs_a_new_calendar is True

    def test_planning_twice_still_writes_nothing(self) -> None:
        gateway = FakeCalendarGateway()

        plan_import(build_settings(), gateway, build_season(*TUESDAYS))
        plan_import(build_settings(), gateway, build_season(*TUESDAYS))

        assert gateway.created == []


class TestAnEmptySeason:
    def test_importing_an_empty_season_does_nothing(self) -> None:
        """A weekday coloured nowhere in the sheet must not crash the import."""
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])

        report = import_training_sessions(build_settings(), gateway, Season(sessions=()))

        assert report.created_event_ids == ()
        assert gateway.events == []

    def test_importing_an_empty_season_creates_no_calendar(self) -> None:
        gateway = FakeCalendarGateway()

        import_training_sessions(build_settings(), gateway, Season(sessions=()))

        assert gateway.created == []


class TestIdentityIsTheSessionDate:
    def test_inserting_a_mid_season_date_does_not_duplicate_the_rest(self) -> None:
        """The ordinal shifts when a date is inserted; the date itself does not."""
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])
        settings = build_settings()
        import_training_sessions(settings, gateway, build_season(*TUESDAYS))

        extended = build_season(*TUESDAYS, date(2026, 9, 8))
        report = import_training_sessions(settings, gateway, extended)

        assert [event.starts_at.date() for event in gateway.events] == [
            *TUESDAYS,
            date(2026, 9, 8),
        ]
        assert len(report.created_event_ids) == 1

    def test_an_event_moved_out_of_the_season_is_still_recognised(self) -> None:
        """A dragged event must not come back as a duplicate."""
        gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])
        settings = build_settings()
        season = build_season(*TUESDAYS)
        import_training_sessions(settings, gateway, season)

        report = import_training_sessions(settings, gateway, season)

        assert report.created_event_ids == ()
        assert len(gateway.events) == len(TUESDAYS)


class TestAPartialFailure:
    def test_reports_how_far_it_got(self) -> None:
        """Re-running is idempotent, but only helps if the user knows some events exist."""
        gateway = FailingCalendarGateway([calendar("cal-1", CALENDAR_NAME)], fail_on_event=3)

        with pytest.raises(PartialImportError, match="created 2 of 4 events"):
            import_training_sessions(build_settings(), gateway, build_season(*TUESDAYS))

    def test_names_the_session_it_failed_on(self) -> None:
        gateway = FailingCalendarGateway([calendar("cal-1", CALENDAR_NAME)], fail_on_event=3)

        with pytest.raises(PartialImportError, match=r"session 3 \(2026-09-29\)"):
            import_training_sessions(build_settings(), gateway, build_season(*TUESDAYS))

    def test_the_events_created_before_the_failure_survive_a_re_run(self) -> None:
        gateway = FailingCalendarGateway([calendar("cal-1", CALENDAR_NAME)], fail_on_event=3)
        season = build_season(*TUESDAYS)
        with pytest.raises(PartialImportError):
            import_training_sessions(build_settings(), gateway, season)

        gateway.fail_on_event = None
        report = import_training_sessions(build_settings(), gateway, season)

        assert len(report.created_event_ids) == 2
        assert [event.starts_at.date() for event in gateway.events] == list(TUESDAYS)
