# RC Wake Check Bot

Posts a wake-up check message to Discord every weekday morning, then posts a
summary (Awake / Asleep / On Trip / Exempt) a few minutes before the 10AM
meeting — accounting for morning trips pulled live from your sign-up sheet.

---

## How it works

- **~9:30 AM (weekdays):** posts a message pinging all male RCs and the
  supervisor, asking them to react with ✅.
- **~9:55 AM (weekdays):** checks who reacted and posts a summary:
  - **Awake** — reacted ✅
  - **Asleep** — didn't react
  - **On Trip** — signed up for something starting before the trip cutoff
    time (default 11:00 AM); shown with their trip's start time and whether
    they responded, with no "asleep" judgment
  - **Exempt** — never tracked
  - **Supervisor** — Henry, tracked separately
  - **Also expected at meeting** — Coleman (owner), informational only

Field trip data is read directly from your existing sign-up sheet (one tab
per day, columns include "Time" and "SIGN UP"). No special formatting/colors
needed — just names and times.

Everything runs for free using **GitHub Actions** — no server, computer, or
Pi needs to stay on.

---

## One-time setup

### 1. Create the Discord bot

1. Go to https://discord.com/developers/applications → **New Application**.
2. Give it a name, create it.
3. Go to the **Bot** tab → click **Reset Token** → copy the token
   (this is `DISCORD_BOT_TOKEN`).
4. On the same **Bot** tab, scroll to **Privileged Gateway Intents** and
   toggle on **Server Members Intent**, then save. (This lets the bot look
   up Discord user IDs from usernames automatically — no manual ID
   collection needed.)
5. Go to **OAuth2 → URL Generator**:
   - Scopes: check `bot`
   - Bot permissions: check `Send Messages`, `Read Message History`,
     `View Channels`
   - Copy the generated URL at the bottom of the page — this is the link
     your boss clicks to add the bot to the server.

### 2. Get the channel ID and server (guild) ID

1. In Discord: **User Settings → Advanced → Developer Mode** (turn on).
2. Right-click the channel where messages should go → **Copy Channel ID** →
   this is `discord_channel_id`.
3. Right-click the server name → **Copy Server ID** → this is
   `discord_guild_id`.

> Note: you can copy these IDs as long as you're a member who can see the
> channel/server — this works even before the bot itself is added.

### 3. Create the Google service account

1. https://console.cloud.google.com/ → create a project.
2. Enable the **Google Sheets API** for it.
3. **APIs & Services → Credentials → Create Credentials → Service Account**.
4. Open it → **Keys** tab → **Add Key → Create new key → JSON** — download
   this file, it's your `GOOGLE_SERVICE_ACCOUNT_JSON`.
5. Note the service account's email (`...@project-id.iam.gserviceaccount.com`).

### 4. Create the BotState Google Sheet

A tiny Google Sheet with one tab named **BotState** (just headers `Key` and
`Value`, leave the rest empty — the bot manages it automatically). Share it
with the service account email (Editor access). Copy its ID from the URL —
this is `config_spreadsheet_id`.

### 5. Share your field trip / sign-up sheet

Share the sheet (the one with daily tabs like "Fri 12", "Sat 13", etc.) with
the same service account email — Viewer access is enough. Copy its ID from
the URL — this is `field_trip_spreadsheet_id`.

> The bot looks for a tab named like `<Weekday> <day-of-month>` (e.g. "Fri
> 12") matching today's date, then reads the "Time" and "SIGN UP" columns by
> header name (so it's fine if column order/extra columns differ between
> tabs).

### 6. Fill in `config.json`

- `discord_channel_id`, `discord_guild_id`
- `field_trip_spreadsheet_id`, `config_spreadsheet_id`

### 7. Check `roster.json`

Already filled in with the male RC roster, exempt list, supervisor, and
owner. Update it any time roster changes — just `name` (full name, used for
matching against the sign-up sheet) and `discord_username` (exact Discord
username, used to find their ID).

### 8. Put it on GitHub

1. Create a new **private** GitHub repository.
2. Upload all files (`main.py`, `sheets_helper.py`, `discord_helper.py`,
   `config.json`, `roster.json`, `requirements.txt`, and the
   `.github/workflows/schedule.yml` — keep the folder structure!).
3. **Settings → Secrets and variables → Actions → New repository secret**:
   - `DISCORD_BOT_TOKEN`
   - `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the entire JSON file contents)

### 9. Send the invite link

Send your boss the OAuth URL from step 1.5. Once they click "Authorize",
the bot appears in the server and the already-scheduled GitHub Actions jobs
will start posting successfully.

---

## Testing

**Actions** tab → **RC Wake Check** → **Run workflow** → choose `morning` or
`summary` → **Run workflow**. Works even before the bot is in a server (the
Sheets-reading half will run; Discord posting will fail until invited).

---

## Adjusting settings (config.json)

- `morning_time` / `summary_time` — when each message posts (Eastern time)
- `trip_cutoff_time` — trips starting strictly before this time go in "On
  Trip" instead of normal Awake/Asleep (default 11:00 AM)
- `time_tolerance_minutes` — handles daylight saving automatically
- `wake_check_emoji` — which reaction counts as "awake"

## Name matching

The bot matches names from the sign-up sheet against `roster.json` using:
full name, first name, and "first name + last initial" (e.g. "Ryan B" →
Ryan Burns, "Ryan D" → Ryan Domsalla). Periods and extra spaces are ignored.
If a new RC's first name collides with an existing one in a way that's
ambiguous, add more specific entries to `roster.json` and let me know — the
alias logic can be extended.
