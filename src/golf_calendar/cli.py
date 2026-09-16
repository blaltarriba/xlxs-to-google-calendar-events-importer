"""Command-line entry point.

Thin by design: parse arguments, delegate, render. Every decision about what a command
means lives below this layer.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from golf_calendar.config import ScheduleSettings
from golf_calendar.config import load_import_settings
from golf_calendar.config import load_schedule_settings
from golf_calendar.config import load_training_details_settings
from golf_calendar.domain import GolfCalendarError
from golf_calendar.domain import Season
from golf_calendar.domain import TrainingSession
from golf_calendar.excel_schedule_reader import read_season
from golf_calendar.google_calendar_gateway import build_google_calendar_gateway
from golf_calendar.import_service import ImportPlan
from golf_calendar.import_service import ImportReport
from golf_calendar.import_service import ensure_calendar
from golf_calendar.import_service import import_training_sessions
from golf_calendar.import_service import plan_import
from golf_calendar.training_details_reader import read_training_details
from golf_calendar.training_details_service import TrainingDetailsPlan
from golf_calendar.training_details_service import apply_training_details
from golf_calendar.training_details_service import plan_training_details


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
    subcommands.add_parser(
        "auth-check",
        help="Sign in to Google, find or create the calendar, and stop. Writes no events.",
    )
    importer = subcommands.add_parser(
        "import",
        help="Create the season's events. Safe to re-run: existing events are skipped.",
    )
    importer.add_argument(
        "--limit",
        type=_positive_count,
        metavar="N",
        help="Create at most N of the missing events, for a cautious first run.",
    )
    importer.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without writing anything.",
    )
    details = subcommands.add_parser(
        "add-training-details",
        help="Add your group's training detail to each imported event. Safe to re-run.",
    )
    details.add_argument(
        "details_file",
        type=Path,
        metavar="FILE",
        help="The trimester's training details spreadsheet.",
    )
    details.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing anything.",
    )
    return parser


def _positive_count(raw: str) -> int:
    """Reject a nonsensical --limit while parsing, before anything signs in to Google."""
    try:
        count = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a whole number") from None
    if count < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {count}")
    return count


def _run(arguments: argparse.Namespace) -> int:
    if arguments.command == "list-sessions":
        return _list_sessions()
    if arguments.command == "auth-check":
        return _auth_check()
    if arguments.command == "import":
        return _import(limit=arguments.limit, dry_run=arguments.dry_run)
    if arguments.command == "add-training-details":
        return _add_training_details(arguments.details_file, dry_run=arguments.dry_run)
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


def _auth_check() -> int:
    settings = load_import_settings()
    gateway = build_google_calendar_gateway(settings)
    ensured = ensure_calendar(gateway, settings.schedule.calendar_name, settings.schedule.timezone)
    state = "created" if ensured.created else "reused"
    print(f"{state} calendar {ensured.calendar.summary!r} (id {ensured.calendar.calendar_id})")
    print(f"signed in, token cached at {settings.token_file}")
    return 0


def _import(limit: int | None, dry_run: bool) -> int:
    settings = load_import_settings()
    season = read_season(
        schedule_file=settings.schedule.schedule_file,
        weekday=settings.schedule.training_weekday,
        extra_session_dates=settings.schedule.extra_session_dates,
    )
    gateway = build_google_calendar_gateway(settings)
    if dry_run:
        print(_format_plan(plan_import(settings, gateway, season, limit)))
        return 0
    report = import_training_sessions(settings, gateway, season, limit)
    print(_format_report(report))
    return 0


def _add_training_details(details_file: Path, dry_run: bool) -> int:
    settings = load_training_details_settings()
    import_settings = settings.import_settings
    season = read_season(
        schedule_file=import_settings.schedule.schedule_file,
        weekday=import_settings.schedule.training_weekday,
        extra_session_dates=import_settings.schedule.extra_session_dates,
    )
    details = read_training_details(details_file, settings.training_group)
    gateway = build_google_calendar_gateway(import_settings)
    if dry_run:
        plan = plan_training_details(import_settings, gateway, season, details)
        print(_format_training_details(plan, update_state="would be updated"))
        return 0
    applied = apply_training_details(import_settings, gateway, season, details)
    print(_format_training_details(applied, update_state="updated"))
    return 0


def _format_training_details(plan: TrainingDetailsPlan, update_state: str) -> str:
    """Render which events carry their detail, and which sessions still lack an event."""
    lines = [
        f"using calendar {plan.calendar.summary!r} (id {plan.calendar.calendar_id})",
        f"{len(plan.already_current)} already up to date, {len(plan.to_update)} {update_state}",
    ]
    lines.extend(
        f"  {_describe(update.session)}  {update.detail.activities}" for update in plan.to_update
    )
    if plan.without_event:
        lines.append(f"{len(plan.without_event)} sessions have no event yet — run `import` first")
        lines.extend(f"  {_describe(session)}" for session in plan.without_event)
    return "\n".join(lines)


def _format_plan(plan: ImportPlan) -> str:
    """Render what an import would do. Nothing here has happened yet."""
    calendar = (
        f"would create calendar {plan.calendar_name!r}"
        if plan.calendar is None
        else f"using calendar {plan.calendar.summary!r} (id {plan.calendar.calendar_id})"
    )
    lines = [
        calendar,
        f"{len(plan.already_present)} already present, {len(plan.to_create)} would be created",
    ]
    lines.extend(f"  {_describe(session)}" for session in plan.to_create)
    if plan.deferred:
        lines.append(f"{len(plan.deferred)} left for a later run (--limit)")
    return "\n".join(lines)


def _format_report(report: ImportReport) -> str:
    plan = report.plan
    if report.calendar is None:
        calendar = f"no calendar needed for {plan.calendar_name!r} — nothing to create"
    else:
        state = "created" if report.calendar_was_created else "using"
        calendar = (
            f"{state} calendar {report.calendar.summary!r} (id {report.calendar.calendar_id})"
        )
    lines = [
        calendar,
        f"{len(plan.already_present)} already present, {len(report.created_event_ids)} created",
    ]
    lines.extend(f"  {_describe(session)}" for session in plan.to_create)
    if plan.deferred:
        lines.append(f"{len(plan.deferred)} left for a later run (--limit)")
    return "\n".join(lines)


def _describe(session: TrainingSession) -> str:
    return f"{session.number:>2}/{session.total}  {session.session_date:%Y-%m-%d}"


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
