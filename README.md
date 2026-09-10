# Note

I used this use case to try Explore/Plan/Code/Commit workflow using Claude Code proposed by Anthropic.


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
3. **APIs & Services → OAuth consent screen** (newer consoles: **Google Auth Platform**) →
   audience type **External**.
4. On the **Audience** page, under **Test users**, click **+ Add users** and add the exact
   Google account you will sign in with. **This step is required** — owning the Cloud
   project does not grant access. Without it the browser shows:

   > Access blocked: … has not completed the Google verification process.
   > Error 403: access_denied

5. **APIs & Services → Credentials → Create credentials → OAuth client ID** →
   application type **Desktop app**.
6. Download the JSON and save it in the project root as `client_secret.json`.

While the consent screen stays in **Testing**, Google expires the refresh token after
**7 days**, so the browser sign-in reappears about weekly. The tool handles that: an
expired or revoked refresh token falls back to a fresh sign-in rather than failing.

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

```sh
uv run golf-calendar import --dry-run     # show what would be created, write nothing
uv run golf-calendar import --limit 2     # create only the first 2 missing events
uv run golf-calendar import               # create every remaining event
```

`--dry-run` writes nothing at all — it does not even create the calendar, so it is safe to
run against a fresh account just to see the plan.

`import` is idempotent. Each event is stamped with a private marker naming its season,
weekday and **session date**, so a re-run recognises what is already there and creates only
the rest — even if you have since renamed, moved or edited an event by hand. The date is
the identity rather than the session number, because inserting one mid-season date into
`EXTRA_SESSION_DATES` renumbers every session after it. That makes `--limit` a safe way to
try a couple of events first and then finish the season, and makes an interrupted run
recoverable by simply running it again.

One limitation: a re-run creates what is missing, it does not update what exists. If you
change `EVENT_TITLE_TEMPLATE` or insert a mid-season date, events already created keep
their original title and numbering.

Events are created with your availability set to **free**, and the guest receives no
invitation email (otherwise one would arrive per session).

## Development

```sh
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```
