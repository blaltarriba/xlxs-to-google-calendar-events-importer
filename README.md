# Golf training calendar importer

Reads the golf school's season spreadsheet (`Calendario 2627 L,M,M.xlsx`) and creates the
season's training sessions in a dedicated Google Calendar.

The spreadsheet is a *visual* month grid: training days are encoded as **cell fill
colours**, not text. The legend row maps each colour to a weekday:

| Fill | Weekday |
|---|---|
| blue `5B9BD5` | Lunes |
| red `E06666` | Martes |
| green `70AD47` | Miércoles |
| yellow `FFF2CC` | Margen / recuperación |
| grey `D9D9D9` | Vacaciones / festivos |

## Setup

```sh
uv sync
cp .env.example .env      # then fill in INVITEE_EMAIL
```

### Google credentials

1. Open the [Google Cloud console](https://console.cloud.google.com/) and create (or pick)
   a project.
2. **APIs & Services → Library** → enable **Google Calendar API**.
3. **APIs & Services → OAuth consent screen** → External, add yourself as a test user.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** →
   application type **Desktop app**.
5. Download the JSON and save it in the project root as `client_secret.json`.

`client_secret.json`, `token.json` and `.env` are gitignored and must never be committed.
The first run opens a browser once; the resulting token is cached in `token.json`.

## Usage

```sh
uv run golf-calendar list-sessions        # parse the sheet and print the season
uv run golf-calendar auth-check           # sign in, find or create the calendar, stop
```

`list-sessions` makes no Google API call and needs no credentials — only `SCHEDULE_FILE`,
`TRAINING_WEEKDAY` and the event settings. It also reports whether the session count was
cross-checked against the total the spreadsheet declares for itself.

`auth-check` is the first command that talks to Google. It performs the OAuth sign-in,
caches the token, then finds or creates the calendar named by `CALENDAR_NAME` and stops
without writing any event. Running it twice must report the same calendar id the second
time, as `reused`. If two calendars already share that name it refuses to guess and names
both ids, rather than scattering a season into the wrong one.

### Not built yet

`import` (step 4) is described in the plan and is not part of this revision. It will be
idempotent: each event carries a private marker naming its session number, so a re-run
skips what already exists — even if the event has since been renamed or moved by hand.

## Development

```sh
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```
