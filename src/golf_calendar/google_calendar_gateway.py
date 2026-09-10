"""Adapter over the Google Calendar API.

This is the only module that imports ``googleapiclient``. It exposes the small port the
service depends on — :class:`CalendarGateway` — so the behaviour above it can be tested
without a network, an account or a browser.

Transport and authentication failures are translated here into :class:`CalendarError`,
preserving the cause, so no library exception escapes into the layers above.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Protocol

from google.auth.exceptions import GoogleAuthError
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error

from golf_calendar.config import ConfigError
from golf_calendar.config import ImportSettings
from golf_calendar.domain import GolfCalendarError

if TYPE_CHECKING:  # `googleapiclient._apis` ships only in the type stubs, not at runtime.
    from googleapiclient._apis.calendar.v3 import CalendarResource

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Owner-only: the cached token grants access to the whole calendar account.
TOKEN_FILE_MODE = 0o600

# calendarList returns subscribed and shared calendars too; only these roles can be
# written to. Reusing a read-only calendar would fail once per event, far too late.
WRITABLE_ACCESS_ROLES = frozenset({"owner", "writer"})


class CalendarError(GolfCalendarError):
    """Raised when Google Calendar rejects a request, or cannot be reached or signed into."""

    code = "calendar_request_failed"


@dataclass(frozen=True, slots=True)
class CalendarRef:
    """A calendar as the API reports it."""

    calendar_id: str
    summary: str
    access_role: str

    @property
    def is_writable(self) -> bool:
        """Whether this account may add events to the calendar."""
        return self.access_role in WRITABLE_ACCESS_ROLES


class CalendarGateway(Protocol):
    """What the import service needs from a calendar account.

    Deliberately narrow: the service decides which calendar to use, so this port only
    fetches and creates. It gains a method when a step actually needs one.
    """

    def list_calendars(self) -> tuple[CalendarRef, ...]:
        """Every calendar on the account."""
        ...

    def create_calendar(self, name: str, timezone: str) -> CalendarRef:
        """Create a calendar and return it."""
        ...


class GoogleCalendarGateway:
    """The live implementation of :class:`CalendarGateway`."""

    def __init__(self, service: CalendarResource) -> None:
        self._service = service

    def list_calendars(self) -> tuple[CalendarRef, ...]:
        """Read the whole calendar list, following every page.

        Stopping at the first page would silently hide a calendar and cause a duplicate
        one to be created, so pagination is followed to the end.
        """
        calendars: list[CalendarRef] = []
        page_token: str | None = None
        while True:
            with _translated_errors():
                response = self._service.calendarList().list(pageToken=page_token).execute()
            calendars.extend(_to_calendar_ref(item) for item in response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return tuple(calendars)

    def create_calendar(self, name: str, timezone: str) -> CalendarRef:
        with _translated_errors():
            created = (
                self._service.calendars()
                .insert(body={"summary": name, "timeZone": timezone})
                .execute()
            )
        # calendars.insert reports no accessRole: the creator always owns the result.
        return _to_calendar_ref(created, default_access_role="owner")


def build_google_calendar_gateway(settings: ImportSettings) -> GoogleCalendarGateway:
    """Sign in if needed and return a gateway bound to the user's account."""
    credentials = load_credentials(settings.client_secret_file, settings.token_file)
    service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    return GoogleCalendarGateway(service)


def load_credentials(client_secret_file: Path, token_file: Path) -> Credentials:
    """Return usable credentials, opening a browser only when there is no other way.

    A cached token is reused as-is, refreshed when it has merely expired, and replaced by
    a fresh sign-in otherwise. The result is always written back.
    """
    cached = _read_cached_credentials(token_file)
    if cached is not None and cached.valid:
        return cached
    credentials = _renewed(cached) or _authorised(client_secret_file)
    _store_credentials(credentials, token_file)
    return credentials


def _read_cached_credentials(token_file: Path) -> Credentials | None:
    if not token_file.is_file():
        return None
    try:
        cached: Credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    except (OSError, ValueError) as error:
        raise ConfigError(
            f"cached token {token_file} is unreadable — delete it to sign in again"
        ) from error
    return cached


def _renewed(cached: Credentials | None) -> Credentials | None:
    """Refresh an expired token, or return ``None`` when signing in again is the only way.

    Google rejects the refresh token once the user revokes the app. Treating that as fatal
    would leave every later run failing identically, so it falls through to a sign-in.
    """
    if cached is None or not cached.expired or not cached.refresh_token:
        return None
    try:
        cached.refresh(Request())
    except RefreshError:
        return None
    except (GoogleAuthError, HttpLib2Error, OSError) as error:
        raise CalendarError(f"could not refresh the cached Google credentials: {error}") from error
    return cached


def _authorised(client_secret_file: Path) -> Credentials:
    if not client_secret_file.is_file():
        raise ConfigError(
            f"OAuth client secrets file {client_secret_file} not found — "
            "download it from the Google Cloud console (see README)"
        )
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), SCOPES)
    except ValueError as error:
        raise ConfigError(
            f"{client_secret_file} is not an OAuth desktop-app client: {error} — "
            "create credentials of type 'Desktop app' (see README)"
        ) from error
    try:
        authorised: Credentials = flow.run_local_server(port=0)
    except (GoogleAuthError, OSError) as error:
        raise CalendarError(f"the browser sign-in did not complete: {error}") from error
    return authorised


def _store_credentials(credentials: Credentials, token_file: Path) -> None:
    """Write the token owner-readable only, never leaving it briefly world-readable.

    Failing here would discard a token just obtained through the browser, so the directory
    is created first and any failure names the path.
    """
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, TOKEN_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as token:
            token.write(credentials.to_json())
        token_file.chmod(TOKEN_FILE_MODE)
    except OSError as error:
        raise ConfigError(f"could not write the token to {token_file}: {error}") from error


@contextmanager
def _translated_errors() -> Iterator[None]:
    """Turn library and transport failures into a domain error at this boundary."""
    try:
        yield
    except HttpError as error:
        raise CalendarError(_describe(error)) from error
    except (GoogleAuthError, HttpLib2Error, OSError) as error:
        # httplib2 raises ServerNotFoundError for a DNS failure, and it is not an OSError.
        raise CalendarError(f"could not reach Google Calendar: {error}") from error


def _describe(error: HttpError) -> str:
    """Name the status as well as the reason.

    401, 403 and 429 need completely different remedies, and ``reason`` is empty when the
    body is not the JSON the client expects.
    """
    reason = error.reason or "no reason given"
    return f"Google Calendar rejected the request ({error.status_code}): {reason}"


def _to_calendar_ref(item: object, default_access_role: str = "") -> CalendarRef:
    if not isinstance(item, dict) or not item.get("id"):
        raise CalendarError(f"Google Calendar returned a calendar without an id: {item!r}")
    return CalendarRef(
        calendar_id=str(item["id"]),
        summary=str(item.get("summary", "")),
        access_role=str(item.get("accessRole", default_access_role)),
    )
