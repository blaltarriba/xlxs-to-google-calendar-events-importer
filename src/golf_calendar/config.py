"""Configuration loaded from the environment (``.env``).

Everything the importer needs that is not the spreadsheet itself lives here. Loading is
strict: a blank, malformed or misspelled value raises :class:`ConfigError` before any
network call is made, rather than surfacing as thirty subtly wrong calendar events.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from datetime import time
from pathlib import Path
from string import Formatter
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from dotenv import load_dotenv

from golf_calendar.domain import GolfCalendarError
from golf_calendar.domain import TrainingWeekday

EVENT_TEMPLATE_PLACEHOLDERS = frozenset({"session", "total", "date", "location"})

_TIME_PATTERN = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})$")


class ConfigError(GolfCalendarError):
    """Raised when the environment does not describe a usable import."""

    code = "config_invalid"


@dataclass(frozen=True, slots=True)
class ScheduleSettings:
    """What it takes to read the season and describe its events.

    Everything here is needed to answer "when is training, and what should the event say?" —
    no Google account required, so ``list-sessions`` can run before any credential exists.
    """

    schedule_file: Path
    training_weekday: TrainingWeekday
    extra_session_dates: tuple[date, ...]
    calendar_name: str
    event_title_template: str
    event_description_template: str
    event_location: str
    start_time: time
    end_time: time
    timezone: str

    def __post_init__(self) -> None:
        if self.end_time <= self.start_time:
            raise ConfigError(
                f"END_TIME ({self.end_time:%H:%M}) must be after "
                f"START_TIME ({self.start_time:%H:%M})"
            )


@dataclass(frozen=True, slots=True)
class ImportSettings:
    """Everything :data:`ScheduleSettings` covers, plus what talking to Google needs."""

    schedule: ScheduleSettings
    invitee_email: str
    client_secret_file: Path
    token_file: Path


@dataclass(frozen=True, slots=True)
class TrainingDetailsSettings:
    """Everything :data:`ImportSettings` covers, plus whose training details to add."""

    import_settings: ImportSettings
    training_group: str


def load_schedule_settings(env_file: Path | None = None) -> ScheduleSettings:
    """Read and validate the settings needed to parse and describe the season."""
    _load_environment(env_file)
    return ScheduleSettings(
        schedule_file=_read_existing_path("SCHEDULE_FILE"),
        training_weekday=_read_weekday("TRAINING_WEEKDAY"),
        extra_session_dates=_read_dates("EXTRA_SESSION_DATES"),
        calendar_name=_read_text("CALENDAR_NAME", default="Golf training"),
        event_title_template=_read_template("EVENT_TITLE_TEMPLATE"),
        event_description_template=_read_template("EVENT_DESCRIPTION_TEMPLATE"),
        event_location=_read_text("EVENT_LOCATION"),
        start_time=_read_time("START_TIME", default="17:30"),
        end_time=_read_time("END_TIME", default="19:30"),
        timezone=_read_timezone("TIMEZONE", default="Europe/Madrid"),
    )


def load_import_settings(env_file: Path | None = None) -> ImportSettings:
    """Read and validate everything needed to write the season to Google Calendar."""
    schedule = load_schedule_settings(env_file)
    return ImportSettings(
        schedule=schedule,
        invitee_email=_read_email("INVITEE_EMAIL"),
        client_secret_file=_read_path("GOOGLE_CLIENT_SECRET_FILE", default="client_secret.json"),
        token_file=_read_path("GOOGLE_TOKEN_FILE", default="token.json"),
    )


def load_training_details_settings(env_file: Path | None = None) -> TrainingDetailsSettings:
    """Read and validate everything needed to add a group's training details to its events.

    ``TRAINING_GROUP`` is demanded only here, so an ``.env`` that predates training details
    still serves every other command.
    """
    return TrainingDetailsSettings(
        import_settings=load_import_settings(env_file),
        training_group=_read_text("TRAINING_GROUP"),
    )


def _load_environment(env_file: Path | None) -> None:
    """Load ``.env`` into the process environment, without letting it win over real values.

    With no argument, python-dotenv searches upward from the working directory. An
    explicitly named file that does not exist is a mistake worth reporting rather than
    quietly falling back to defaults.
    """
    if env_file is not None and not env_file.is_file():
        raise ConfigError(f"env file {env_file} does not exist")
    load_dotenv(dotenv_path=env_file, override=False)


def _read_text(name: str, default: str | None = None) -> str:
    raw = os.environ.get(name, default if default is not None else "")
    value = raw.strip()
    if not value:
        raise ConfigError(f"{name} is required and must not be blank")
    return value


def _read_email(name: str) -> str:
    value = _read_text(name)
    if value.count("@") != 1 or value.startswith("@") or value.endswith("@") or " " in value:
        raise ConfigError(f"{name} is not a valid email address: {value!r}")
    return value


def _read_path(name: str, default: str | None = None) -> Path:
    return Path(_read_text(name, default))


def _read_existing_path(name: str) -> Path:
    path = _read_path(name)
    if not path.is_file():
        raise ConfigError(f"{name} points at {path} which is not a readable file")
    return path


def _read_weekday(name: str) -> TrainingWeekday:
    return TrainingWeekday.from_key(_read_text(name))


def _read_time(name: str, default: str) -> time:
    value = _read_text(name, default)
    match = _TIME_PATTERN.match(value)
    if match is None:
        raise ConfigError(f"{name} must be HH:MM, got {value!r}")
    hour, minute = int(match["hour"]), int(match["minute"])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"{name} is not a valid time of day: {value!r}")
    return time(hour=hour, minute=minute)


def _read_timezone(name: str, default: str) -> str:
    value = _read_text(name, default)
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ConfigError(f"{name} is not a known IANA timezone: {value!r}") from error
    return value


def _read_template(name: str) -> str:
    template = _read_text(name).replace("\\n", "\n")
    used = {field for _, field, _, _ in Formatter().parse(template) if field is not None}
    unknown = used - EVENT_TEMPLATE_PLACEHOLDERS
    if unknown:
        listed = ", ".join(f"{{{field}}}" for field in sorted(unknown))
        known = ", ".join(sorted(EVENT_TEMPLATE_PLACEHOLDERS))
        raise ConfigError(f"{name} uses unknown placeholder(s) {listed}; known: {known}")
    return template


def _read_dates(name: str) -> tuple[date, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return ()
    parsed: list[date] = []
    for entry in raw.split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        try:
            parsed.append(date.fromisoformat(candidate))
        except ValueError as error:
            raise ConfigError(
                f"{name} entry {candidate!r} is not an ISO date (YYYY-MM-DD)"
            ) from error
    return tuple(parsed)
