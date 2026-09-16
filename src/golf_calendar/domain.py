"""Domain model for a golf-school training season.

A season is an ordered run of :class:`TrainingSession` values, all falling on the same
:class:`TrainingWeekday`. Nothing here touches the spreadsheet, the network or the clock.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import Enum


class GolfCalendarError(Exception):
    """Base class for every failure this package raises.

    Each subclass carries a stable ``code`` that is part of the public contract and is
    safe to branch on; the message is for humans and may change.
    """

    code: str = "golf_calendar_error"


class InvalidSeasonError(GolfCalendarError):
    """Raised when a set of dates cannot form a coherent season."""

    code = "invalid_season"


class UnknownTrainingWeekdayError(GolfCalendarError):
    """Raised when a configured weekday is not one the school trains on."""

    code = "unknown_training_weekday"


class TrainingWeekday(Enum):
    """A weekday on which the school runs training.

    The 2026/2027 season runs Monday, Tuesday and Wednesday only. Each member carries the
    Spanish label used in the spreadsheet legend and the index ``date.weekday()`` returns,
    so callers never restate either mapping.
    """

    key: str
    legend_label: str
    weekday_index: int

    MONDAY = ("monday", "Lunes", 0)
    TUESDAY = ("tuesday", "Martes", 1)
    WEDNESDAY = ("wednesday", "Miércoles", 2)

    def __init__(self, key: str, legend_label: str, weekday_index: int) -> None:
        self.key = key
        self.legend_label = legend_label
        self.weekday_index = weekday_index

    @classmethod
    def from_key(cls, key: str) -> TrainingWeekday:
        """Resolve the English key used in configuration, case- and space-insensitively."""
        normalised = key.strip().lower()
        for weekday in cls:
            if weekday.key == normalised:
                return weekday
        known = ", ".join(weekday.key for weekday in cls)
        raise UnknownTrainingWeekdayError(f"unknown training weekday {key!r}; known: {known}")

    def matches(self, day: date) -> bool:
        """Whether ``day`` falls on this weekday."""
        return day.weekday() == self.weekday_index


@dataclass(frozen=True, slots=True)
class TrainingSession:
    """One numbered training session of a season."""

    session_date: date
    number: int
    total: int

    def __post_init__(self) -> None:
        if self.total < 1:
            raise InvalidSeasonError(f"season total must be positive, got {self.total}")
        if not 1 <= self.number <= self.total:
            raise InvalidSeasonError(f"session number {self.number} outside 1..{self.total}")


@dataclass(frozen=True, slots=True)
class TrainingDetail:
    """What one group practises on one session date, e.g. ``Putt + Juego largo``.

    ``group_label`` is the group's heading exactly as the school's sheet writes it, so it can
    be shown back to the reader without restating the sheet's wording here.
    """

    session_date: date
    group_label: str
    activities: str


def number_sessions(session_dates: Iterable[date]) -> tuple[TrainingSession, ...]:
    """Order ``session_dates`` and number them ``1..N`` of ``N``.

    Duplicates are a caller bug rather than something to quietly absorb, so they raise.
    """
    ordered = sorted(session_dates)
    duplicates = {day for day in ordered if ordered.count(day) > 1}
    if duplicates:
        listed = ", ".join(day.isoformat() for day in sorted(duplicates))
        raise InvalidSeasonError(f"duplicate session dates: {listed}")
    total = len(ordered)
    return tuple(
        TrainingSession(session_date=day, number=index, total=total)
        for index, day in enumerate(ordered, start=1)
    )


@dataclass(frozen=True, slots=True)
class Season:
    """A numbered run of sessions, plus the total the source claimed for it.

    ``declared_total`` is what the schedule stated about itself, or ``None`` when it stated
    nothing. Keeping it alongside the sessions lets callers report whether the count was
    cross-checked at all, rather than a silently-skipped check passing for a passed one.
    """

    sessions: tuple[TrainingSession, ...]
    declared_total: int | None = None

    def __len__(self) -> int:
        return len(self.sessions)

    @property
    def is_cross_checked(self) -> bool:
        """Whether the source declared a total for the count to be verified against."""
        return self.declared_total is not None

    @property
    def key(self) -> str:
        """A stable identifier for the season, e.g. ``2026-2027``.

        Derived from the sessions themselves rather than from a filename, so renaming the
        source spreadsheet cannot make a re-run fail to recognise its own past events.
        """
        if not self.sessions:
            raise InvalidSeasonError("an empty season has no key")
        first = self.sessions[0].session_date.year
        last = self.sessions[-1].session_date.year
        return str(first) if first == last else f"{first}-{last}"
