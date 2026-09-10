"""Tests for environment-driven configuration."""

from datetime import date
from datetime import time
from pathlib import Path

import pytest

from golf_calendar.config import ConfigError
from golf_calendar.config import ImportSettings
from golf_calendar.config import ScheduleSettings
from golf_calendar.config import load_import_settings
from golf_calendar.config import load_schedule_settings
from golf_calendar.domain import TrainingWeekday

VALID_ENVIRONMENT = {
    "SCHEDULE_FILE": "schedule.xlsx",
    "TRAINING_WEEKDAY": "tuesday",
    "CALENDAR_NAME": "Golf training",
    "INVITEE_EMAIL": "wife@example.com",
    "EVENT_TITLE_TEMPLATE": "Entrenamiento {session}/{total}",
    "EVENT_DESCRIPTION_TEMPLATE": "Entrenamiento\\nSesión {session} de {total}",
    "EVENT_LOCATION": "Test location, Test City",
    "START_TIME": "17:30",
    "END_TIME": "19:30",
    "TIMEZONE": "Europe/Madrid",
    "GOOGLE_CLIENT_SECRET_FILE": "client_secret.json",
    "GOOGLE_TOKEN_FILE": "token.json",
    "EXTRA_SESSION_DATES": "2027-06-01",
}


EMPTY_ENV_FILE = Path("empty.env")


@pytest.fixture(autouse=True)
def _valid_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Put a schedule file on disk and a complete, valid environment in place.

    An empty ``.env`` keeps a real one on the developer's machine out of the test.
    """
    (tmp_path / "schedule.xlsx").write_bytes(b"")
    (tmp_path / EMPTY_ENV_FILE).write_text("")
    monkeypatch.chdir(tmp_path)
    for name, value in VALID_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


def load() -> ImportSettings:
    """Load every setting, from the environment only."""
    return load_import_settings(env_file=EMPTY_ENV_FILE)


def load_schedule() -> ScheduleSettings:
    """Load only the settings needed to read and describe the season."""
    return load_schedule_settings(env_file=EMPTY_ENV_FILE)


class TestLoadSettings:
    def test_reads_a_complete_environment(self) -> None:
        settings = load()

        assert settings == ImportSettings(
            schedule=ScheduleSettings(
                schedule_file=Path("schedule.xlsx"),
                training_weekday=TrainingWeekday.TUESDAY,
                extra_session_dates=(date(2027, 6, 1),),
                calendar_name="Golf training",
                event_title_template="Entrenamiento {session}/{total}",
                event_description_template=("Entrenamiento\nSesión {session} de {total}"),
                event_location="Test location, Test City",
                start_time=time(17, 30),
                end_time=time(19, 30),
                timezone="Europe/Madrid",
            ),
            invitee_email="wife@example.com",
            client_secret_file=Path("client_secret.json"),
            token_file=Path("token.json"),
        )

    def test_decodes_escaped_newlines_in_the_description(self) -> None:
        template = load_schedule().event_description_template

        assert template == "Entrenamiento\nSesión {session} de {total}"


class TestRequiredValues:
    @pytest.mark.parametrize(
        "name",
        [
            "SCHEDULE_FILE",
            "TRAINING_WEEKDAY",
            "INVITEE_EMAIL",
            "CALENDAR_NAME",
            "EVENT_TITLE_TEMPLATE",
            "EVENT_DESCRIPTION_TEMPLATE",
            "EVENT_LOCATION",
        ],
    )
    def test_blank_required_value_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        monkeypatch.setenv(name, "   ")

        with pytest.raises(ConfigError, match=name):
            load()

    def test_missing_schedule_file_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCHEDULE_FILE", "nowhere.xlsx")

        with pytest.raises(ConfigError, match="not a readable file"):
            load()

    @pytest.mark.parametrize("email", ["wife", "wife@", "@example.com", "a b@example.com"])
    def test_malformed_invitee_email_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, email: str
    ) -> None:
        monkeypatch.setenv("INVITEE_EMAIL", email)

        with pytest.raises(ConfigError, match="not a valid email address"):
            load()


class TestTemplates:
    def test_unknown_placeholder_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVENT_TITLE_TEMPLATE", "Golf {sesion}/{total}")

        with pytest.raises(ConfigError, match=r"unknown placeholder\(s\) \{sesion\}"):
            load()

    @pytest.mark.parametrize("template", ["Golf {}", "Golf {0}", "Golf {1}/{total}"])
    def test_positional_placeholder_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, template: str
    ) -> None:
        """These render as an IndexError mid-import, so they must fail at load instead."""
        monkeypatch.setenv("EVENT_TITLE_TEMPLATE", template)

        with pytest.raises(ConfigError, match="unknown placeholder"):
            load()

    @pytest.mark.parametrize("placeholder", ["session", "total", "date", "location"])
    def test_every_documented_placeholder_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch, placeholder: str
    ) -> None:
        monkeypatch.setenv("EVENT_TITLE_TEMPLATE", f"Golf {{{placeholder}}}")

        assert load_schedule().event_title_template == f"Golf {{{placeholder}}}"

    def test_a_template_without_placeholders_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVENT_TITLE_TEMPLATE", "Entrenamiento")

        assert load_schedule().event_title_template == "Entrenamiento"


class TestTimes:
    @pytest.mark.parametrize("value", ["17", "17:3", "25:00", "17:60", "half five", "17:30:00"])
    def test_malformed_time_is_rejected(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("START_TIME", value)

        with pytest.raises(ConfigError, match="START_TIME"):
            load()

    def test_end_before_start_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("END_TIME", "16:00")

        with pytest.raises(ConfigError, match="must be after"):
            load()

    def test_unknown_timezone_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TIMEZONE", "Europe/Valencia")

        with pytest.raises(ConfigError, match="not a known IANA timezone"):
            load()


class TestEnvFile:
    def test_a_named_env_file_that_does_not_exist_is_rejected(self) -> None:
        """Silently falling back to defaults would hide a mistyped path."""
        with pytest.raises(ConfigError, match=r"env file nowhere\.env does not exist"):
            load_import_settings(env_file=Path("nowhere.env"))


class TestScheduleSettings:
    def test_reading_the_season_does_not_require_google_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`list-sessions` makes no Google call, so it must not demand an invitee."""
        monkeypatch.delenv("INVITEE_EMAIL")

        assert load_schedule().training_weekday is TrainingWeekday.TUESDAY

    def test_importing_does_require_an_invitee(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("INVITEE_EMAIL")

        with pytest.raises(ConfigError, match="INVITEE_EMAIL"):
            load()


class TestExtraSessionDates:
    def test_absent_value_yields_no_extra_dates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EXTRA_SESSION_DATES")

        assert load_schedule().extra_session_dates == ()

    def test_several_dates_are_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EXTRA_SESSION_DATES", "2027-06-01, 2027-06-08 ,")

        assert load_schedule().extra_session_dates == (date(2027, 6, 1), date(2027, 6, 8))

    def test_malformed_date_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EXTRA_SESSION_DATES", "01/06/2027")

        with pytest.raises(ConfigError, match="not an ISO date"):
            load()
