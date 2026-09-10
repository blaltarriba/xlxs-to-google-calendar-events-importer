"""Tests for the season domain model."""

from datetime import date

import pytest

from golf_calendar.domain import InvalidSeasonError
from golf_calendar.domain import Season
from golf_calendar.domain import TrainingSession
from golf_calendar.domain import TrainingWeekday
from golf_calendar.domain import UnknownTrainingWeekdayError
from golf_calendar.domain import number_sessions


class TestTrainingWeekday:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("monday", TrainingWeekday.MONDAY),
            ("tuesday", TrainingWeekday.TUESDAY),
            ("wednesday", TrainingWeekday.WEDNESDAY),
            ("  Tuesday  ", TrainingWeekday.TUESDAY),
            ("TUESDAY", TrainingWeekday.TUESDAY),
        ],
    )
    def test_resolves_configured_key(self, key: str, expected: TrainingWeekday) -> None:
        assert TrainingWeekday.from_key(key) is expected

    @pytest.mark.parametrize("key", ["thursday", "jueves", "martes", ""])
    def test_rejects_a_weekday_the_school_does_not_train_on(self, key: str) -> None:
        with pytest.raises(UnknownTrainingWeekdayError):
            TrainingWeekday.from_key(key)

    def test_carries_the_spreadsheet_legend_label(self) -> None:
        assert TrainingWeekday.TUESDAY.legend_label == "Martes"

    @pytest.mark.parametrize(
        ("weekday", "day", "expected"),
        [
            (TrainingWeekday.TUESDAY, date(2026, 9, 15), True),
            (TrainingWeekday.TUESDAY, date(2026, 9, 16), False),
            (TrainingWeekday.MONDAY, date(2026, 9, 14), True),
            (TrainingWeekday.WEDNESDAY, date(2026, 9, 16), True),
        ],
    )
    def test_matches_dates_falling_on_it(
        self, weekday: TrainingWeekday, day: date, expected: bool
    ) -> None:
        assert weekday.matches(day) is expected


class TestTrainingSession:
    def test_rejects_a_number_outside_the_season(self) -> None:
        with pytest.raises(InvalidSeasonError, match=r"outside 1\.\.30"):
            TrainingSession(session_date=date(2026, 9, 15), number=31, total=30)

    def test_rejects_a_zero_number(self) -> None:
        with pytest.raises(InvalidSeasonError, match=r"outside 1\.\.30"):
            TrainingSession(session_date=date(2026, 9, 15), number=0, total=30)

    def test_rejects_an_empty_season(self) -> None:
        with pytest.raises(InvalidSeasonError, match="total must be positive"):
            TrainingSession(session_date=date(2026, 9, 15), number=1, total=0)


class TestNumberSessions:
    def test_orders_and_numbers_the_whole_season(self) -> None:
        unordered = [date(2026, 9, 22), date(2026, 9, 15), date(2026, 9, 29)]

        sessions = number_sessions(unordered)

        assert sessions == (
            TrainingSession(session_date=date(2026, 9, 15), number=1, total=3),
            TrainingSession(session_date=date(2026, 9, 22), number=2, total=3),
            TrainingSession(session_date=date(2026, 9, 29), number=3, total=3),
        )

    def test_returns_nothing_for_an_empty_season(self) -> None:
        assert number_sessions([]) == ()

    def test_rejects_duplicate_dates(self) -> None:
        with pytest.raises(InvalidSeasonError, match="duplicate session dates: 2026-09-15"):
            number_sessions([date(2026, 9, 15), date(2026, 9, 15)])


class TestSeason:
    def test_reports_that_a_declared_total_was_cross_checked(self) -> None:
        season = Season(sessions=number_sessions([date(2026, 9, 15)]), declared_total=1)

        assert season.is_cross_checked is True
        assert len(season) == 1

    def test_reports_when_no_total_was_declared(self) -> None:
        season = Season(sessions=number_sessions([date(2026, 9, 15)]))

        assert season.is_cross_checked is False

    def test_keys_a_season_by_the_years_its_sessions_span(self) -> None:
        season = Season(sessions=number_sessions([date(2026, 9, 15), date(2027, 6, 8)]))

        assert season.key == "2026-2027"

    def test_keys_a_single_year_season_by_that_year(self) -> None:
        season = Season(sessions=number_sessions([date(2026, 9, 15), date(2026, 9, 22)]))

        assert season.key == "2026"

    def test_an_empty_season_has_no_key(self) -> None:
        with pytest.raises(InvalidSeasonError, match="empty season has no key"):
            _ = Season(sessions=()).key
