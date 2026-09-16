"""Tests for adding training details to imported events, driven through a fake gateway."""

from __future__ import annotations

from datetime import date

import pytest

from golf_calendar.domain import Season
from golf_calendar.domain import TrainingDetail
from golf_calendar.google_calendar_gateway import CalendarError
from golf_calendar.import_service import import_training_sessions
from golf_calendar.training_details_service import CalendarNotFoundError
from golf_calendar.training_details_service import DetailUpdate
from golf_calendar.training_details_service import PartialTrainingDetailsError
from golf_calendar.training_details_service import TrainingDetailsPlan
from golf_calendar.training_details_service import UnknownSessionDateError
from golf_calendar.training_details_service import apply_training_details
from golf_calendar.training_details_service import plan_training_details
from tests.golf_calendar.test_import_service import CALENDAR_NAME
from tests.golf_calendar.test_import_service import FakeCalendarGateway
from tests.golf_calendar.test_import_service import build_season
from tests.golf_calendar.test_import_service import build_settings
from tests.golf_calendar.test_import_service import calendar

FIRST = date(2026, 9, 15)
SECOND = date(2026, 9, 22)


def detail(day: date, activities: str) -> TrainingDetail:
    return TrainingDetail(session_date=day, group_label="Grupo E", activities=activities)


def imported_calendar(season: Season, limit: int | None = None) -> FakeCalendarGateway:
    """An account whose calendar already holds the season's imported events."""
    gateway = FakeCalendarGateway([calendar("cal-1", CALENDAR_NAME)])
    import_training_sessions(build_settings(), gateway, season, limit)
    gateway.queries.clear()
    return gateway


class FailingUpdateGateway(FakeCalendarGateway):
    """Fails when asked for the nth update, to model an interrupted run."""

    fail_on_update: int | None = None

    def update_event_text(
        self, calendar_id: str, event_id: str, summary: str, description: str
    ) -> None:
        if len(self.updates) + 1 == self.fail_on_update:
            raise CalendarError("Google Calendar rejected the request (503): backend error")
        super().update_event_text(calendar_id, event_id, summary, description)


class TestPlansEachUpdate:
    def test_plans_each_detail_into_the_title_and_description_of_its_event(self) -> None:
        season = build_season(FIRST, SECOND)
        gateway = imported_calendar(season)
        details = (detail(FIRST, "Putt + Juego largo"), detail(SECOND, "Approach + Putt"))

        plan = plan_training_details(build_settings(), gateway, season, details)

        assert plan == TrainingDetailsPlan(
            calendar=calendar("cal-1", CALENDAR_NAME),
            to_update=(
                DetailUpdate(
                    session=season.sessions[0],
                    detail=details[0],
                    event_id="cal-1-event-1",
                    summary="Entrenamiento 1/2 · Putt + Juego largo",
                    description="Entrenamiento\nSesión 1 de 2\n\nGrupo E: Putt + Juego largo",
                ),
                DetailUpdate(
                    session=season.sessions[1],
                    detail=details[1],
                    event_id="cal-1-event-2",
                    summary="Entrenamiento 2/2 · Approach + Putt",
                    description="Entrenamiento\nSesión 2 de 2\n\nGrupo E: Approach + Putt",
                ),
            ),
            already_current=(),
            without_event=(),
        )

    def test_reports_a_session_whose_event_was_not_imported_yet(self) -> None:
        season = build_season(FIRST, SECOND)
        gateway = imported_calendar(season, limit=1)
        details = (detail(FIRST, "Putt"), detail(SECOND, "Approach"))

        plan = plan_training_details(build_settings(), gateway, season, details)

        assert plan.without_event == (season.sessions[1],)
        assert [update.event_id for update in plan.to_update] == ["cal-1-event-1"]

    def test_asks_for_the_imported_events_only_once(self) -> None:
        season = build_season(FIRST, SECOND)
        gateway = imported_calendar(season)
        details = (detail(FIRST, "Putt"), detail(SECOND, "Approach"))

        plan_training_details(build_settings(), gateway, season, details)

        assert gateway.queries == [("cal-1", "golf-training-2026-tuesday")]

    def test_no_details_plan_nothing(self) -> None:
        season = build_season(FIRST)
        gateway = imported_calendar(season)

        plan = plan_training_details(build_settings(), gateway, season, ())

        assert (plan.to_update, plan.already_current, plan.without_event) == ((), (), ())
        assert gateway.updates == []

    def test_an_event_already_carrying_its_detail_needs_no_update(self) -> None:
        season = build_season(FIRST)
        gateway = imported_calendar(season)
        gateway.update_event_text(
            "cal-1",
            "cal-1-event-1",
            "Entrenamiento 1/1 · Putt",
            "Entrenamiento\nSesión 1 de 1\n\nGrupo E: Putt",
        )

        plan = plan_training_details(build_settings(), gateway, season, (detail(FIRST, "Putt"),))

        assert plan.to_update == ()
        assert plan.already_current == season.sessions


class TestApplyTrainingDetails:
    def test_writes_each_planned_update_to_its_event(self) -> None:
        season = build_season(FIRST, SECOND)
        gateway = imported_calendar(season)
        details = (detail(FIRST, "Putt"), detail(SECOND, "Approach"))

        plan = apply_training_details(build_settings(), gateway, season, details)

        assert gateway.updates == [
            ("cal-1", update.event_id, update.summary, update.description)
            for update in plan.to_update
        ]
        assert len(gateway.updates) == 2

    def test_a_second_run_updates_nothing(self) -> None:
        """The whole point: re-running must be safe and write nothing."""
        season = build_season(FIRST, SECOND)
        gateway = imported_calendar(season)
        details = (detail(FIRST, "Putt"), detail(SECOND, "Approach"))
        apply_training_details(build_settings(), gateway, season, details)

        plan = apply_training_details(build_settings(), gateway, season, details)

        assert plan.to_update == ()
        assert plan.already_current == season.sessions
        assert len(gateway.updates) == 2

    def test_a_corrected_detail_replaces_the_old_one_rather_than_stacking(self) -> None:
        season = build_season(FIRST)
        gateway = imported_calendar(season)
        apply_training_details(build_settings(), gateway, season, (detail(FIRST, "Putt"),))

        apply_training_details(build_settings(), gateway, season, (detail(FIRST, "Approach"),))

        assert gateway.updates[-1] == (
            "cal-1",
            "cal-1-event-1",
            "Entrenamiento 1/1 · Approach",
            "Entrenamiento\nSesión 1 de 1\n\nGrupo E: Approach",
        )

    def test_planning_alone_writes_nothing(self) -> None:
        """--dry-run reports the plan, so planning must not touch any event."""
        season = build_season(FIRST)
        gateway = imported_calendar(season)

        plan = plan_training_details(build_settings(), gateway, season, (detail(FIRST, "Putt"),))

        assert len(plan.to_update) == 1
        assert gateway.updates == []

    def test_reports_how_far_it_got_when_an_update_fails(self) -> None:
        season = build_season(FIRST, SECOND)
        gateway = FailingUpdateGateway([calendar("cal-1", CALENDAR_NAME)])
        import_training_sessions(build_settings(), gateway, season)
        gateway.fail_on_update = 2
        details = (detail(FIRST, "Putt"), detail(SECOND, "Approach"))

        with pytest.raises(PartialTrainingDetailsError, match=r"updated 1 of 2 .* re-run"):
            apply_training_details(build_settings(), gateway, season, details)


class TestRefusesToGuess:
    def test_a_detail_for_a_date_that_is_not_a_session(self) -> None:
        """The two spreadsheets disagree, so nothing should be asked or written at all."""
        season = build_season(FIRST)
        gateway = imported_calendar(season)
        details = (detail(FIRST, "Putt"), detail(date(2026, 9, 16), "Approach"))

        with pytest.raises(UnknownSessionDateError, match="2026-09-16 is not a session"):
            apply_training_details(build_settings(), gateway, season, details)

        assert gateway.updates == []
        assert gateway.queries == []

    def test_a_calendar_that_does_not_exist(self) -> None:
        season = build_season(FIRST)
        gateway = FakeCalendarGateway()

        with pytest.raises(CalendarNotFoundError, match="run `import` first"):
            plan_training_details(build_settings(), gateway, season, (detail(FIRST, "Putt"),))
