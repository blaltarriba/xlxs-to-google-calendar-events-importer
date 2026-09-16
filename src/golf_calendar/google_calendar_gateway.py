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
from datetime import date
from datetime import datetime
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
from oauthlib.oauth2.rfc6749.errors import AccessDeniedError
from oauthlib.oauth2.rfc6749.errors import OAuth2Error

from golf_calendar.config import ConfigError
from golf_calendar.config import ImportSettings
from golf_calendar.domain import GolfCalendarError

if TYPE_CHECKING:  # `googleapiclient._apis` ships only in the type stubs, not at runtime.
    from googleapiclient._apis.calendar.v3 import CalendarResource
    from googleapiclient._apis.calendar.v3 import Event

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Owner-only: the cached token grants access to the whole calendar account.
TOKEN_FILE_MODE = 0o600

# calendarList returns subscribed and shared calendars too; only these roles can be
# written to. Reusing a read-only calendar would fail once per event, far too late.
WRITABLE_ACCESS_ROLES = frozenset({"owner", "writer"})

# Private extended properties stamped on every event this tool creates. They are what a
# re-run matches against, so they survive the event being renamed, moved or edited by hand.
IMPORTER_PROPERTY = "importer"
SESSION_PROPERTY = "session"
# The session's own date, and the identity a re-run matches on. The ordinal cannot serve:
# inserting one mid-season date renumbers every session after it, which would make a
# second run duplicate the tail of the season and miss the new date entirely.
SESSION_DATE_PROPERTY = "session_date"

# Google caps a page of events at 2500; 250 is its own default and plenty per season.
EVENTS_PAGE_SIZE = 250


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


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """An event to create, stated in local wall-clock time.

    ``starts_at`` and ``ends_at`` are naive on purpose: paired with ``timezone`` they let
    Google resolve the offset itself, so 17:30 stays 17:30 either side of a daylight-saving
    change. A baked-in UTC offset would drift by an hour for half the season.
    """

    summary: str
    description: str
    location: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    invitees: tuple[str, ...]
    shows_as_busy: bool
    import_key: str
    session_number: int


@dataclass(frozen=True, slots=True)
class ImportedEvent:
    """An event this importer wrote earlier, as the calendar holds it now."""

    event_id: str
    session_date: date
    summary: str
    description: str


class CalendarGateway(Protocol):
    """What the services need from a calendar account.

    Deliberately narrow: the service decides which calendar to use, so this port only
    fetches, creates and retitles. It gains a method when a step actually needs one.
    """

    def list_calendars(self) -> tuple[CalendarRef, ...]:
        """Every calendar on the account."""
        ...

    def create_calendar(self, name: str, timezone: str) -> CalendarRef:
        """Create a calendar and return it."""
        ...

    def list_imported_dates(self, calendar_id: str, import_key: str) -> frozenset[date]:
        """The session dates this importer has already written into the calendar."""
        ...

    def list_imported_events(self, calendar_id: str, import_key: str) -> tuple[ImportedEvent, ...]:
        """The events this importer has already written into the calendar."""
        ...

    def update_event_text(
        self, calendar_id: str, event_id: str, summary: str, description: str
    ) -> None:
        """Replace one event's title and description, leaving every other field alone."""
        ...

    def create_event(self, calendar_id: str, event: CalendarEvent) -> str:
        """Create one event and return its id."""
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

    def list_imported_dates(self, calendar_id: str, import_key: str) -> frozenset[date]:
        return frozenset(_session_dates(self._imported_items(calendar_id, import_key)))

    def list_imported_events(self, calendar_id: str, import_key: str) -> tuple[ImportedEvent, ...]:
        items = self._imported_items(calendar_id, import_key)
        return tuple(event for item in items if (event := _to_imported_event(item)) is not None)

    def update_event_text(
        self, calendar_id: str, event_id: str, summary: str, description: str
    ) -> None:
        with _translated_errors():
            self._service.events().patch(
                calendarId=calendar_id,
                eventId=event_id,
                body={"summary": summary, "description": description},
                # The guest would otherwise receive one update email per session.
                sendUpdates="none",
            ).execute()

    def _imported_items(self, calendar_id: str, import_key: str) -> list[object]:
        """Ask once for everything this importer has already written, following every page.

        The marker alone scopes the query, so no time window is imposed: bounding the
        search to the season's own span would miss an event the user had dragged outside
        it, and duplicate it on the next run.
        """
        items: list[object] = []
        page_token: str | None = None
        while True:
            with _translated_errors():
                response = (
                    self._service.events()
                    .list(
                        calendarId=calendar_id,
                        privateExtendedProperty=[f"{IMPORTER_PROPERTY}={import_key}"],
                        showDeleted=False,
                        maxResults=EVENTS_PAGE_SIZE,
                        pageToken=page_token,
                    )
                    .execute()
                )
            items.extend(response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return items

    def create_event(self, calendar_id: str, event: CalendarEvent) -> str:
        with _translated_errors():
            created = (
                self._service.events()
                .insert(
                    calendarId=calendar_id,
                    body=_to_event_body(event),
                    # The guest would otherwise receive one invitation email per session.
                    sendUpdates="none",
                )
                .execute()
            )
        event_id = str(created.get("id") or "")
        if not event_id:
            raise CalendarError("Google Calendar created an event but returned no id")
        return event_id


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
    except AccessDeniedError as error:
        # Overwhelmingly this is an unlisted test user rather than a deliberate refusal.
        raise ConfigError(
            "Google refused the sign-in (access_denied). While the OAuth consent screen is "
            "in Testing, only listed test users may sign in: add the account you signed in "
            "with under APIs & Services > OAuth consent screen > Audience > Test users. "
            "If you pressed Cancel instead, just run the command again"
        ) from error
    except (OAuth2Error, GoogleAuthError, OSError) as error:
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


def _to_event_body(event: CalendarEvent) -> Event:
    """Render the event in the shape the API expects. The only place that shape is known."""
    return {
        "summary": event.summary,
        "description": event.description,
        "location": event.location,
        "start": {"dateTime": event.starts_at.isoformat(), "timeZone": event.timezone},
        "end": {"dateTime": event.ends_at.isoformat(), "timeZone": event.timezone},
        "attendees": [{"email": invitee} for invitee in event.invitees],
        "transparency": "opaque" if event.shows_as_busy else "transparent",
        "extendedProperties": {
            "private": {
                IMPORTER_PROPERTY: event.import_key,
                SESSION_PROPERTY: str(event.session_number),
                SESSION_DATE_PROPERTY: event.starts_at.date().isoformat(),
            }
        },
    }


def _session_dates(items: list[object]) -> set[date]:
    """Read the session date off each event, ignoring anything that does not carry one."""
    return {day for item in items if (day := _session_date_of(item)) is not None}


def _to_imported_event(item: object) -> ImportedEvent | None:
    """Read one listed event, or ``None`` when it lacks an id or a usable session date."""
    session_date = _session_date_of(item)
    if not isinstance(item, dict) or not item.get("id") or session_date is None:
        return None
    return ImportedEvent(
        event_id=str(item["id"]),
        session_date=session_date,
        summary=str(item.get("summary", "")),
        description=str(item.get("description", "")),
    )


def _session_date_of(item: object) -> date | None:
    marked = _private_properties(item).get(SESSION_DATE_PROPERTY)
    if not isinstance(marked, str):
        return None
    try:
        return date.fromisoformat(marked)
    except ValueError:
        return None


def _private_properties(item: object) -> dict[str, object]:
    """The event's private extended properties, defensively — the API is not our code."""
    if not isinstance(item, dict):
        return {}
    properties = item.get("extendedProperties")
    if not isinstance(properties, dict):
        return {}
    private = properties.get("private")
    return private if isinstance(private, dict) else {}


def _to_calendar_ref(item: object, default_access_role: str = "") -> CalendarRef:
    if not isinstance(item, dict) or not item.get("id"):
        raise CalendarError(f"Google Calendar returned a calendar without an id: {item!r}")
    return CalendarRef(
        calendar_id=str(item["id"]),
        summary=str(item.get("summary", "")),
        access_role=str(item.get("accessRole", default_access_role)),
    )
