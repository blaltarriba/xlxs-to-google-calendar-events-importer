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

## Development

```sh
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```
