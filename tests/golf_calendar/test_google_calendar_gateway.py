"""Tests for the Google Calendar adapter.

The transport is stubbed, so these assert the request that was sent and how the response
was parsed without a network, an account or a browser.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from httplib2 import Response
from httplib2.error import ServerNotFoundError

from golf_calendar.config import ConfigError
from golf_calendar.google_calendar_gateway import CalendarError
from golf_calendar.google_calendar_gateway import CalendarRef
from golf_calendar.google_calendar_gateway import GoogleCalendarGateway
from golf_calendar.google_calendar_gateway import load_credentials

CALENDAR_NAME = "Golf training"
TIMEZONE = "Europe/Madrid"

Outcome = tuple[Response, bytes] | Exception


def ok(payload: dict[str, Any]) -> Outcome:
    return Response({"status": "200"}), json.dumps(payload).encode()


def refused(status: int, message: str) -> Outcome:
    body = json.dumps({"error": {"code": status, "message": message}}).encode()
    return Response({"status": str(status)}), body


class StubbedRequest:
    def __init__(self, outcome: Outcome, postproc: Any) -> None:
        self._outcome = outcome
        self._postproc = postproc

    def execute(self, _http: Any = None) -> Any:
        if isinstance(self._outcome, Exception):
            raise self._outcome
        response, content = self._outcome
        return self._postproc(response, content)


class StubbedTransport:
    """Returns queued outcomes per API method and records every request sent."""

    def __init__(self, outcomes: dict[str, list[Outcome]]) -> None:
        self._queues = {method: list(queue) for method, queue in outcomes.items()}
        self.requests: list[tuple[str, dict[str, Any] | None]] = []

    def __call__(self, _http: Any, postproc: Any, _uri: str, **request: Any) -> StubbedRequest:
        method = str(request.get("methodId") or "")
        body = request.get("body")
        self.requests.append((method, json.loads(body) if body else None))
        queue = self._queues.get(method)
        if not queue:
            raise AssertionError(f"unexpected call to {method}")
        return StubbedRequest(queue.pop(0), postproc)

    def bodies_for(self, method: str) -> list[dict[str, Any] | None]:
        return [body for sent_method, body in self.requests if sent_method == method]

    def call_count(self, method: str) -> int:
        return sum(1 for sent_method, _ in self.requests if sent_method == method)


def gateway_for(transport: StubbedTransport) -> GoogleCalendarGateway:
    """Build a real calendar service whose transport is the stub.

    ``static_discovery`` uses the discovery document bundled with the library, so no
    network is touched. The stub stands in for googleapiclient's private request-builder
    protocol, which insists on returning a concrete ``HttpRequest``.
    """
    service = build(  # type: ignore[call-overload]
        "calendar",
        "v3",
        requestBuilder=transport,
        developerKey="test",
        static_discovery=True,
    )
    return GoogleCalendarGateway(service)


class TestListCalendars:
    def test_parses_the_calendars_the_api_returns(self) -> None:
        transport = StubbedTransport(
            {
                "calendar.calendarList.list": [
                    ok(
                        {
                            "items": [
                                {"id": "cal-1", "summary": "Personal", "accessRole": "owner"},
                                {"id": "cal-2", "summary": CALENDAR_NAME, "accessRole": "writer"},
                            ]
                        }
                    )
                ]
            }
        )

        calendars = gateway_for(transport).list_calendars()

        assert calendars == (
            CalendarRef(calendar_id="cal-1", summary="Personal", access_role="owner"),
            CalendarRef(calendar_id="cal-2", summary=CALENDAR_NAME, access_role="writer"),
        )

    def test_follows_every_page(self) -> None:
        """Stopping at page one would hide a calendar and cause a duplicate to be created."""
        transport = StubbedTransport(
            {
                "calendar.calendarList.list": [
                    ok({"items": [{"id": "cal-1", "summary": "Personal"}], "nextPageToken": "p2"}),
                    ok({"items": [{"id": "cal-2", "summary": "Trabajo"}], "nextPageToken": "p3"}),
                    ok({"items": [{"id": "cal-3", "summary": CALENDAR_NAME}]}),
                ]
            }
        )

        calendars = gateway_for(transport).list_calendars()

        assert [calendar.calendar_id for calendar in calendars] == ["cal-1", "cal-2", "cal-3"]
        assert transport.call_count("calendar.calendarList.list") == 3

    def test_reads_an_empty_account(self) -> None:
        transport = StubbedTransport({"calendar.calendarList.list": [ok({})]})

        assert gateway_for(transport).list_calendars() == ()

    def test_defaults_a_calendar_without_a_summary_to_an_empty_name(self) -> None:
        transport = StubbedTransport(
            {"calendar.calendarList.list": [ok({"items": [{"id": "cal-1"}]})]}
        )

        calendars = gateway_for(transport).list_calendars()

        assert calendars == (CalendarRef(calendar_id="cal-1", summary="", access_role=""),)

    @pytest.mark.parametrize("role", ["owner", "writer", "reader", "freeBusyReader"])
    def test_carries_the_access_role_through(self, role: str) -> None:
        """The service needs it to tell a calendar it owns from one merely shared with it."""
        transport = StubbedTransport(
            {
                "calendar.calendarList.list": [
                    ok({"items": [{"id": "cal-1", "summary": CALENDAR_NAME, "accessRole": role}]})
                ]
            }
        )

        calendars = gateway_for(transport).list_calendars()

        assert calendars[0].access_role == role
        assert calendars[0].is_writable is (role in {"owner", "writer"})

    def test_rejects_a_calendar_without_an_id(self) -> None:
        transport = StubbedTransport(
            {"calendar.calendarList.list": [ok({"items": [{"summary": CALENDAR_NAME}]})]}
        )

        with pytest.raises(CalendarError, match="without an id"):
            gateway_for(transport).list_calendars()


class TestCreateCalendar:
    def test_sends_the_name_and_timezone(self) -> None:
        transport = StubbedTransport(
            {"calendar.calendars.insert": [ok({"id": "new-1", "summary": CALENDAR_NAME})]}
        )

        gateway_for(transport).create_calendar(CALENDAR_NAME, TIMEZONE)

        assert transport.bodies_for("calendar.calendars.insert") == [
            {"summary": CALENDAR_NAME, "timeZone": TIMEZONE}
        ]

    def test_a_calendar_it_just_created_is_owned(self) -> None:
        """calendars.insert reports no accessRole, but the creator always owns the result."""
        transport = StubbedTransport(
            {"calendar.calendars.insert": [ok({"id": "new-1", "summary": CALENDAR_NAME})]}
        )

        created = gateway_for(transport).create_calendar(CALENDAR_NAME, TIMEZONE)

        assert created.is_writable is True

    def test_returns_the_created_calendar(self) -> None:
        transport = StubbedTransport(
            {"calendar.calendars.insert": [ok({"id": "new-1", "summary": CALENDAR_NAME})]}
        )

        created = gateway_for(transport).create_calendar(CALENDAR_NAME, TIMEZONE)

        assert created == CalendarRef(
            calendar_id="new-1", summary=CALENDAR_NAME, access_role="owner"
        )


class TestTranslatesFailures:
    @pytest.mark.parametrize(
        ("status", "message"),
        [
            (403, "Insufficient permission"),
            (404, "Not Found"),
            (429, "Rate Limit Exceeded"),
            (500, "Backend Error"),
        ],
    )
    def test_an_api_refusal_becomes_a_domain_error(self, status: int, message: str) -> None:
        transport = StubbedTransport({"calendar.calendarList.list": [refused(status, message)]})

        with pytest.raises(CalendarError, match="Google Calendar rejected the request"):
            gateway_for(transport).list_calendars()

    def test_the_original_failure_is_preserved_as_the_cause(self) -> None:
        transport = StubbedTransport(
            {"calendar.calendarList.list": [refused(403, "Insufficient permission")]}
        )

        with pytest.raises(CalendarError) as raised:
            gateway_for(transport).list_calendars()

        assert raised.value.__cause__ is not None
        assert "Insufficient permission" in str(raised.value)

    def test_an_offline_machine_becomes_a_domain_error(self) -> None:
        """httplib2 raises ServerNotFoundError for a DNS failure, and it is not an OSError."""
        transport = StubbedTransport(
            {
                "calendar.calendarList.list": [
                    ServerNotFoundError("Unable to find the server at www.googleapis.com")
                ]
            }
        )

        with pytest.raises(CalendarError, match="could not reach Google Calendar"):
            gateway_for(transport).list_calendars()

    def test_a_socket_failure_becomes_a_domain_error(self) -> None:
        transport = StubbedTransport({"calendar.calendarList.list": [OSError("connection reset")]})

        with pytest.raises(CalendarError, match="could not reach Google Calendar"):
            gateway_for(transport).list_calendars()

    @pytest.mark.parametrize(
        ("status", "message"),
        [(401, "Invalid Credentials"), (403, "Insufficient permission"), (429, "Rate Limit")],
    )
    def test_the_status_code_survives_into_the_message(self, status: int, message: str) -> None:
        """401, 403 and 429 need completely different remedies."""
        transport = StubbedTransport({"calendar.calendarList.list": [refused(status, message)]})

        with pytest.raises(CalendarError, match=f"\\({status}\\): {message}"):
            gateway_for(transport).list_calendars()

    def test_a_refusal_with_an_unparseable_body_still_names_the_status(self) -> None:
        """An HTML error page yields no JSON reason, so the status is all the user gets."""
        transport = StubbedTransport(
            {
                "calendar.calendarList.list": [
                    (Response({"status": "503"}), b"<html>Service Unavailable</html>")
                ]
            }
        )

        with pytest.raises(CalendarError, match=r"\(503\)"):
            gateway_for(transport).list_calendars()

    def test_a_failure_while_creating_is_translated_too(self) -> None:
        transport = StubbedTransport(
            {"calendar.calendars.insert": [refused(403, "Insufficient permission")]}
        )

        with pytest.raises(CalendarError, match="Google Calendar rejected the request"):
            gateway_for(transport).create_calendar(CALENDAR_NAME, TIMEZONE)


FUTURE = "2099-01-01T00:00:00Z"
PAST = "2020-01-01T00:00:00Z"


def write_token(token_file: Path, *, expiry: str = FUTURE) -> None:
    """Write a cached-token file.

    ``expiry`` is always stated: google-auth defaults a missing expiry to *now*, which
    reads back as already expired.
    """
    payload: dict[str, Any] = {
        "token": "cached-access-token",
        "refresh_token": "cached-refresh-token",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "scopes": ["https://www.googleapis.com/auth/calendar"],
        "universe_domain": "googleapis.com",
        "expiry": expiry,
    }
    token_file.write_text(json.dumps(payload), encoding="utf-8")


class StubbedSignIn:
    """Stands in for the browser OAuth flow."""

    @classmethod
    def from_client_secrets_file(cls, _secret_file: str, _scopes: list[str]) -> StubbedSignIn:
        return cls()

    def run_local_server(self, port: int = 0) -> Credentials:  # noqa: ARG002
        return Credentials(
            token="freshly-signed-in",
            refresh_token="refresh",
            client_id="client-id",
            client_secret="client-secret",
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/calendar"],
        )


class TestLoadCredentials:
    def test_a_valid_cached_token_is_reused_without_signing_in(self, tmp_path: Path) -> None:
        """No browser may open on an ordinary run."""
        token_file = tmp_path / "token.json"
        write_token(token_file)

        credentials = load_credentials(tmp_path / "absent-secret.json", token_file)

        assert credentials.token == "cached-access-token"

    def test_an_expired_token_is_refreshed_and_written_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token_file = tmp_path / "token.json"
        write_token(token_file, expiry=PAST)

        def refresh(self: Any, request: Any) -> None:  # noqa: ARG001
            self.token = "refreshed-access-token"
            self.expiry = None

        monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", refresh)

        credentials = load_credentials(tmp_path / "absent-secret.json", token_file)

        assert credentials.token == "refreshed-access-token"
        assert json.loads(token_file.read_text())["token"] == "refreshed-access-token"

    def test_a_missing_client_secret_names_the_file_it_wants(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"client_secret\.json not found"):
            load_credentials(tmp_path / "client_secret.json", tmp_path / "absent-token.json")

    def test_an_unreadable_cached_token_says_how_to_recover(self, tmp_path: Path) -> None:
        token_file = tmp_path / "token.json"
        token_file.write_text("not json", encoding="utf-8")

        with pytest.raises(ConfigError, match="delete it to sign in again"):
            load_credentials(tmp_path / "client_secret.json", token_file)

    def test_a_revoked_refresh_token_leads_back_to_a_sign_in(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise every later run fails identically with no way out."""
        token_file = tmp_path / "token.json"
        write_token(token_file, expiry=PAST)

        def refuse(self: Any, request: Any) -> None:  # noqa: ARG001
            raise RefreshError("Token has been expired or revoked.")

        monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", refuse)

        with pytest.raises(ConfigError, match="not found"):
            load_credentials(tmp_path / "client_secret.json", token_file)

    def test_a_transient_refresh_failure_is_not_mistaken_for_a_revocation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A network blip must not silently open a browser."""
        token_file = tmp_path / "token.json"
        write_token(token_file, expiry=PAST)

        def fail(self: Any, request: Any) -> None:  # noqa: ARG001
            raise ServerNotFoundError("Unable to find the server")

        monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", fail)

        with pytest.raises(CalendarError, match="could not refresh"):
            load_credentials(tmp_path / "client_secret.json", token_file)

    def test_a_client_secret_of_the_wrong_kind_says_what_to_download(self, tmp_path: Path) -> None:
        """A service-account key downloaded by mistake must not raise a bare ValueError."""
        secret_file = tmp_path / "client_secret.json"
        secret_file.write_text(json.dumps({"type": "service_account"}), encoding="utf-8")

        with pytest.raises(ConfigError, match="not an OAuth desktop-app client"):
            load_credentials(secret_file, tmp_path / "absent-token.json")

    def test_the_token_directory_is_created_if_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A GOOGLE_TOKEN_FILE under a new directory must not discard a fresh sign-in."""
        secret_file = tmp_path / "client_secret.json"
        secret_file.write_text("{}", encoding="utf-8")
        token_file = tmp_path / "nested" / "deeper" / "token.json"
        monkeypatch.setattr("golf_calendar.google_calendar_gateway.InstalledAppFlow", StubbedSignIn)

        credentials = load_credentials(secret_file, token_file)

        assert credentials.token == "freshly-signed-in"
        assert json.loads(token_file.read_text())["token"] == "freshly-signed-in"

    def test_a_written_token_is_readable_only_by_its_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The token grants access to the whole calendar account."""
        token_file = tmp_path / "token.json"
        write_token(token_file, expiry=PAST)
        token_file.chmod(0o644)

        def refresh(self: Any, request: Any) -> None:  # noqa: ARG001
            self.expiry = None

        monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", refresh)

        load_credentials(tmp_path / "absent-secret.json", token_file)

        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
