"""
sheets_helper.py
Handles Google Sheets interactions:
  - BotState tab (stores message ID between the morning/summary runs)
  - Scanning today's tab on the field trip sheet (name + time based, no colors)
"""

import re
import time
import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

WEEKDAY_ABBR = {0: "Mon", 1: "Tues", 2: "Wed", 3: "Thurs", 4: "Fri", 5: "Sat", 6: "Sun"}


def with_retry(func, *args, retries=3, delay=3, **kwargs):
    """
    Calls func(*args, **kwargs), retrying on transient gspread API errors
    (e.g. occasional 500/503 'Internal error' responses from Google).
    """
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as exc:
            last_exc = exc
            status = None
            try:
                status = exc.response.status_code
            except Exception:
                pass
            if status is not None and status < 500:
                raise  # don't retry on 4xx (auth/permission/not-found etc.)
            if attempt < retries:
                print(f"Sheets API error (attempt {attempt}/{retries}): {exc} - retrying in {delay}s")
                time.sleep(delay)
    raise last_exc


def get_client(creds_path="service_account.json"):
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    return gspread.authorize(creds)


# ---------------------------------------------------------------------------
# BotState tab helpers
# ---------------------------------------------------------------------------

def get_state(spreadsheet, key, default=None):
    ws = with_retry(spreadsheet.worksheet, "BotState")
    records = with_retry(ws.get_all_records)
    for r in records:
        if str(r.get("Key", "")).strip() == key:
            return str(r.get("Value", "")).strip()
    return default


def set_state(spreadsheet, key, value):
    ws = with_retry(spreadsheet.worksheet, "BotState")
    records = with_retry(ws.get_all_records)
    for i, r in enumerate(records):
        if str(r.get("Key", "")).strip() == key:
            with_retry(ws.update_cell, i + 2, 2, str(value))
            return
    with_retry(ws.append_row, [key, str(value)])


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

def normalize(s):
    s = s.lower().strip()
    s = s.replace(".", "")
    s = re.sub(r"\s+", " ", s)
    return s


def build_alias_map(people):
    """
    people: list of {"name": "Ryan Burns", ...}
    Returns dict normalized_alias -> full name, covering:
      - full name ("ryan burns")
      - first name only ("ryan")
      - first name + last initial ("ryan b")
    """
    alias_map = {}
    for p in people:
        name = p["name"]
        parts = name.split()
        first = parts[0]
        aliases = {name, first}
        if len(parts) > 1:
            last = parts[-1]
            aliases.add(f"{first} {last[0]}")
        for a in aliases:
            key = normalize(a)
            # don't let a later, less-specific alias overwrite an earlier exact match
            if key not in alias_map:
                alias_map[key] = name
    return alias_map


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)


def _parse_time_token(token):
    m = _TIME_RE.search(token.strip())
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    ampm = ampm.lower() if ampm else None
    return hour, minute, ampm


def _to_minutes(hour, minute, ampm):
    h = hour % 12
    if ampm == "pm":
        h += 12
    return h * 60 + minute


def parse_time_range_to_minutes(text):
    """
    Parses strings like '8:00-5:00 PM', '12:45-4:15 PM', '10:00 AM', '4:30 PM'.
    Returns (start_minutes, end_minutes) since midnight, or None if unparseable.
    """
    if not text:
        return None
    text = text.strip()
    if not text or text.upper() in ("N/A", "TBD"):
        return None

    parts = re.split(r"-", text, maxsplit=1)

    if len(parts) == 1:
        t = _parse_time_token(parts[0])
        if t is None:
            return None
        hour, minute, ampm = t
        if ampm is None:
            ampm = "pm" if hour == 12 else "am"
        mins = _to_minutes(hour, minute, ampm)
        return mins, mins

    start_t = _parse_time_token(parts[0])
    end_t = _parse_time_token(parts[1])
    if start_t is None or end_t is None:
        return None

    sh, sm, sampm = start_t
    eh, em, eampm = end_t

    if eampm is None:
        eampm = "am" if eh == 12 else "pm"
    end_min = _to_minutes(eh, em, eampm)

    if sampm is None:
        if sh == 12:
            sampm = "pm"
        else:
            cand_am = _to_minutes(sh, sm, "am")
            sampm = "am" if cand_am <= end_min else "pm"

    start_min = _to_minutes(sh, sm, sampm)
    return start_min, end_min


# ---------------------------------------------------------------------------
# Field trip sheet
# ---------------------------------------------------------------------------

def _find_today_worksheet(spreadsheet, today):
    weekday_abbr = WEEKDAY_ABBR[today.weekday()]
    candidates = {
        f"{weekday_abbr} {today.day}".lower(),
        f"{today.strftime('%a')} {today.day}".lower(),
    }
    print(f"DEBUG: Looking for tab matching {candidates}")

    all_tabs = with_retry(spreadsheet.worksheets)
    print(f"DEBUG: Available tabs = {[ws.title for ws in all_tabs]}")
    for ws in all_tabs:
        if ws.title.strip().lower() in candidates:
            return ws

    return None


def get_morning_trip_people(spreadsheet, today, alias_map, cutoff_time_minutes):
    """
    Scans today's tab. Returns a dict {full_name: start_minutes} for anyone
    signed up for something that starts BEFORE `cutoff_time_minutes`
    (i.e. a morning trip). A trip starting exactly at the cutoff or later
    is not included. End time doesn't matter.
    """
    trip_people = {}

    ws = _find_today_worksheet(spreadsheet, today)
    if ws is None:
        return trip_people

    rows = with_retry(ws.get_all_values)
    if not rows or len(rows) < 2:
        return trip_people

    # Row 0 is the date label, row 1 is the actual header row
    headers = [h.strip().lower() for h in rows[1]]
    try:
        time_idx = headers.index("time")
        signup_idx = headers.index("sign up")
    except ValueError:
        return trip_people

    current_range = None
    for row in rows[2:]:  # data starts at row 3 (index 2)
        if len(row) <= max(time_idx, signup_idx):
            row = row + [""] * (max(time_idx, signup_idx) + 1 - len(row))

        time_text = row[time_idx].strip()
        if time_text:
            parsed = parse_time_range_to_minutes(time_text)
            if parsed is not None:
                current_range = parsed

        signup_text = row[signup_idx].strip()
        if not signup_text or signup_text.upper() == "ALL":
            continue
        if current_range is None:
            continue

        start_min, _end_min = current_range
        if start_min >= cutoff_time_minutes:
            continue

        for name_piece in re.split(r"[\n,]", signup_text):
            name_piece = name_piece.strip()
            if not name_piece:
                continue
            matched = alias_map.get(normalize(name_piece))
            if matched:
                # if someone appears on multiple morning trips, keep the earliest
                if matched not in trip_people or start_min < trip_people[matched]:
                    trip_people[matched] = start_min

    return trip_people


def minutes_to_time_str(total_minutes):
    """Formats minutes-since-midnight as e.g. '9:45 AM'."""
    h = (total_minutes // 60) % 24
    m = total_minutes % 60
    period = "AM" if h < 12 else "PM"
    h12 = h % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{m:02d} {period}"


# ---------------------------------------------------------------------------
# Full-day schedule (for event reminders)
# ---------------------------------------------------------------------------

def get_day_schedule(spreadsheet, today, alias_map):
    """
    Scans today's tab and returns a list of events:
      {
        "event_name": str,
        "time_text": str,
        "start_minutes": int,
        "transportation": str,
        "notes": str,
        "people": [full_name, ...]   # sorted, only tracked people
      }
    Only includes events that have a parseable start time AND at least one
    tracked person signed up.
    """
    ws = _find_today_worksheet(spreadsheet, today)
    if ws is None:
        print(f"DEBUG: No worksheet found for {today} - tried '{WEEKDAY_ABBR[today.weekday()]} {today.day}'")
        return []

    print(f"DEBUG: Found worksheet '{ws.title}'")
    rows = with_retry(ws.get_all_values)
    print(f"DEBUG: Got {len(rows)} rows")
    if not rows:
        return []

    # Row 0 is the date label (e.g. "Tuesday, June 16"), row 1 is the header
    if len(rows) < 2:
        return []
    headers = [h.strip().lower() for h in rows[1]]
    print(f"DEBUG: Headers = {headers}")
    try:
        event_idx = headers.index("event")
        time_idx = headers.index("time")
        signup_idx = headers.index("sign up")
    except ValueError as e:
        print(f"DEBUG: Missing required header - {e}")
        return []

    transport_idx = headers.index("transportation") if "transportation" in headers else None
    notes_idx = headers.index("notes") if "notes" in headers else None

    needed_idx = [event_idx, time_idx, signup_idx]
    if transport_idx is not None:
        needed_idx.append(transport_idx)
    if notes_idx is not None:
        needed_idx.append(notes_idx)
    max_idx = max(needed_idx)

    events = []
    current = None

    for row in rows[2:]:  # data starts at row 3 (index 2)
        if len(row) <= max_idx:
            row = row + [""] * (max_idx + 1 - len(row))

        event_name = row[event_idx].strip()
        time_text = row[time_idx].strip()

        if event_name or time_text:
            parsed = parse_time_range_to_minutes(time_text) if time_text else None
            current = {
                "event_name": event_name,
                "time_text": time_text,
                "start_minutes": parsed[0] if parsed else None,
                "transportation": row[transport_idx].strip() if transport_idx is not None else "",
                "notes": row[notes_idx].strip() if notes_idx is not None else "",
                "people": set(),
            }
            events.append(current)

        signup_text = row[signup_idx].strip()
        if signup_text and signup_text.upper() != "ALL" and current is not None:
            for piece in re.split(r"[\n,]", signup_text):
                piece = piece.strip()
                if not piece:
                    continue
                matched = alias_map.get(normalize(piece))
                if matched:
                    current["people"].add(matched)

    result = []
    for e in events:
        if e["start_minutes"] is None:
            continue
        if not e["people"]:
            continue
        e["people"] = sorted(e["people"])
        result.append(e)

    return result
