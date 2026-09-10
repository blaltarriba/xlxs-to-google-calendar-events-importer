"""Command-line entry point.

Thin by design: parse arguments, delegate, render. Every decision about what a command
means lives below this layer.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from golf_calendar.config import ScheduleSettings
from golf_calendar.config import load_schedule_settings
from golf_calendar.domain import GolfCalendarError
from golf_calendar.domain import Season
from golf_calendar.domain import TrainingSession
from golf_calendar.excel_schedule_reader import read_season


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command, translating any domain failure into a message and exit code 1."""
    arguments = _build_parser().parse_args(argv)
    try:
        return _run(arguments)
    except GolfCalendarError as error:
        print(f"error [{error.code}]: {error}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="golf-calendar",
        description="Import the golf school season schedule into Google Calendar.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser(
        "list-sessions",
        help="Parse the spreadsheet and print the season. Makes no Google API call.",
    )
    return parser


def _run(arguments: argparse.Namespace) -> int:
    if arguments.command == "list-sessions":
        return _list_sessions()
    raise GolfCalendarError(f"unhandled command {arguments.command!r}")


def _list_sessions() -> int:
    settings = load_schedule_settings()
    season = read_season(
        schedule_file=settings.schedule_file,
        weekday=settings.training_weekday,
        extra_session_dates=settings.extra_session_dates,
    )
    print(_format_season_heading(settings, season))
    for session in season.sessions:
        print(_format_session(session, settings))
    print(_format_cross_check(season))
    return 0


def _format_season_heading(settings: ScheduleSettings, season: Season) -> str:
    weekday = settings.training_weekday.legend_label
    return (
        f"{settings.calendar_name} — {weekday} — {len(season)} sessions "
        f"({settings.start_time:%H:%M}–{settings.end_time:%H:%M} {settings.timezone})"
    )


def _format_session(session: TrainingSession, settings: ScheduleSettings) -> str:
    weekday = settings.training_weekday.legend_label
    return f"  {session.number:>2}/{session.total}  {session.session_date:%Y-%m-%d}  {weekday}"


def _format_cross_check(season: Season) -> str:
    """Say whether the count was verified, so a skipped check never reads as a passed one."""
    if season.declared_total is None:
        return "\nnote: the sheet declares no season total, so the count was not cross-checked."
    return f"\nCount cross-checked against the sheet's declared total of {season.declared_total}."


if __name__ == "__main__":
    raise SystemExit(main())
