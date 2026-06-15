"""
main.py
Entry point. Run with one argument: "morning", "summary", or "reminders".

Environment variables required:
  DISCORD_BOT_TOKEN           - your bot's token
  GOOGLE_SERVICE_ACCOUNT_FILE - path to the Google service account JSON
                                 (defaults to "service_account.json")

Reads settings from config.json and roster.json (same folder).
"""

import sys
import os
import json
import datetime
from zoneinfo import ZoneInfo

import sheets_helper
import discord_helper


def in_time_window(target_time_str, timezone_str, tolerance_minutes):
    tz = ZoneInfo(timezone_str)
    now = datetime.datetime.now(tz)
    target_hour, target_minute = (int(x) for x in target_time_str.split(":"))
    target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    diff_minutes = abs((now - target).total_seconds()) / 60
    return diff_minutes <= tolerance_minutes


def in_active_window(start_time_str, end_time_str, timezone_str):
    """True if the current local time is between start_time_str and end_time_str (inclusive)."""
    tz = ZoneInfo(timezone_str)
    now = datetime.datetime.now(tz)
    now_minutes = now.hour * 60 + now.minute
    start_minutes = _time_str_to_minutes(start_time_str)
    end_minutes = _time_str_to_minutes(end_time_str)
    return start_minutes <= now_minutes <= end_minutes


def _time_str_to_minutes(time_str):
    hour, minute = time_str.split(":")
    return int(hour) * 60 + int(minute)


def load_json(filename):
    with open(os.path.join(os.path.dirname(__file__), filename)) as f:
        return json.load(f)


def parse_meeting_time_to_minutes(meeting_time_str):
    hour, minute = meeting_time_str.split(":")
    return int(hour) * 60 + int(minute)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("morning", "summary", "reminders"):
        print("Usage: python main.py [morning|summary|reminders]")
        sys.exit(1)

    mode = sys.argv[1]
    cfg = load_json("config.json")
    roster_cfg = load_json("roster.json")
    dry_run = os.environ.get("DRY_RUN", "").lower() == "true"

    if not dry_run and datetime.date.today().weekday() >= 5:
        print("It's the weekend - skipping this run.")
        return

    if mode in ("morning", "summary"):
        target_time = cfg["morning_time"] if mode == "morning" else cfg["summary_time"]
        if not dry_run and not in_time_window(target_time, cfg["timezone"], cfg["time_tolerance_minutes"]):
            print(f"Not within the {target_time} {cfg['timezone']} window - skipping this run.")
            return
    else:  # reminders
        if not dry_run and not in_active_window(
            cfg["reminder_window_start"], cfg["reminder_window_end"], cfg["timezone"]
        ):
            print("Outside the reminder active window - skipping this run.")
            return

    bot_token = os.environ["DISCORD_BOT_TOKEN"]
    creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")

    client = sheets_helper.get_client(creds_path)
    config_sheet = sheets_helper.with_retry(client.open_by_key, cfg["config_spreadsheet_id"])
    trip_sheet = sheets_helper.with_retry(client.open_by_key, cfg["field_trip_spreadsheet_id"])

    roster = roster_cfg["roster"]
    female_roster = roster_cfg.get("female_roster", [])
    other_staff = roster_cfg.get("other_staff", [])
    supervisor = roster_cfg["supervisor"]
    owner = roster_cfg["owner"]

    # People tracked for wake-up/summary (guys only - Henry/supervisor handled separately)
    all_tracked = roster

    # People tracked for reminders (everyone)
    all_tracked_for_reminders = roster + female_roster + supervisor + other_staff + owner

    today = datetime.date.today()

    if mode == "reminders":
        alias_map = sheets_helper.build_alias_map(all_tracked_for_reminders)
        run_reminders(cfg, bot_token, config_sheet, trip_sheet, all_tracked_for_reminders, alias_map, today, dry_run)
        return

    alias_map = sheets_helper.build_alias_map(all_tracked)

    cutoff_minutes = parse_meeting_time_to_minutes(cfg["trip_cutoff_time"])
    trip_people_today = sheets_helper.get_morning_trip_people(
        spreadsheet=trip_sheet,
        today=today,
        alias_map=alias_map,
        cutoff_time_minutes=cutoff_minutes,
    )

    if mode == "morning":
        run_morning(cfg, bot_token, config_sheet, roster, trip_people_today, today, dry_run)
    else:
        run_summary(cfg, bot_token, config_sheet, roster, supervisor, owner, trip_people_today, today, dry_run)


def run_morning(cfg, bot_token, config_sheet, roster, trip_people_today, today, dry_run=False):
    to_ping = roster

    if dry_run:
        print("=== DRY RUN: morning ===")
        print(f"Today: {today.isoformat()} ({today.strftime('%A')})")
        print(f"Would ping {len(to_ping)} people:")
        for p in to_ping:
            print(f"  - {p['name']} (@{p['discord_username']})")
        print()
        print("Morning message text:")
        print(cfg["morning_message"])
        print()
        print("Trip people detected today (name: start time):")
        if trip_people_today:
            for name, mins in trip_people_today.items():
                print(f"  - {name}: {sheets_helper.minutes_to_time_str(mins)}")
        else:
            print("  (none)")
        print()
        print("Writing test state to BotState sheet...")
        sheets_helper.set_state(config_sheet, "last_message_id", "DRY_RUN")
        sheets_helper.set_state(config_sheet, "last_message_date", today.isoformat())
        sheets_helper.set_state(config_sheet, "last_trip_people", json.dumps(trip_people_today))
        print("Done - check the BotState tab to confirm it updated.")
        return

    username_map = discord_helper.build_username_to_id_map(bot_token, cfg["discord_guild_id"])

    mention_parts = []
    for p in to_ping:
        uid = username_map.get(p["discord_username"].lower())
        if uid:
            mention_parts.append(f"<@{uid}>")
        else:
            mention_parts.append(f"@{p['discord_username']}")

    if mention_parts:
        content = " ".join(mention_parts) + "\n\n" + cfg["morning_message"]
    else:
        content = cfg["morning_message"]

    message_id = discord_helper.post_message(bot_token, cfg["discord_channel_id"], content)

    sheets_helper.set_state(config_sheet, "last_message_id", message_id)
    sheets_helper.set_state(config_sheet, "last_message_date", today.isoformat())
    sheets_helper.set_state(config_sheet, "last_trip_people", json.dumps(trip_people_today))

    print(f"Posted morning message {message_id}")


def run_summary(cfg, bot_token, config_sheet, roster, supervisor, owner, trip_people_today, today, dry_run=False):
    saved_date = sheets_helper.get_state(config_sheet, "last_message_date")
    message_id = sheets_helper.get_state(config_sheet, "last_message_id")

    if saved_date != today.isoformat() or not message_id:
        print("No morning message found for today - skipping summary.")
        return

    saved_trip_raw = sheets_helper.get_state(config_sheet, "last_trip_people", "{}")
    try:
        trip_people_today = json.loads(saved_trip_raw)
    except (TypeError, ValueError):
        pass

    if dry_run:
        print("=== DRY RUN: summary ===")
        print(f"Today: {today.isoformat()} ({today.strftime('%A')})")
        print(f"Saved state - message_id: {message_id}, date: {saved_date}")
        print()
        print("Trip people (from saved state):")
        if trip_people_today:
            for name, mins in trip_people_today.items():
                print(f"  - {name}: {sheets_helper.minutes_to_time_str(mins)}")
        else:
            print("  (none)")
        print()
        print("Roster:")
        for p in roster:
            in_trip = p["name"] in trip_people_today
            print(f"  - {p['name']} (@{p['discord_username']}) - {'ON TRIP' if in_trip else 'normal'}")
        print()
        print("Owner:", ", ".join(p["name"] for p in owner) or "(none)")
        print("Supervisor (tagged in Asleep section if anyone is asleep):",
              ", ".join(p["name"] for p in supervisor) or "(none)")
        print()
        print("(Skipping reaction check and message posting - bot not in server yet)")
        return

    reacted_ids = discord_helper.get_reaction_user_ids(
        bot_token, cfg["discord_channel_id"], message_id, cfg["wake_check_emoji"]
    )
    username_map = discord_helper.build_username_to_id_map(bot_token, cfg["discord_guild_id"])

    def reacted(p):
        uid = username_map.get(p["discord_username"].lower())
        return bool(uid and uid in reacted_ids)

    def evaluate(people):
        awake, asleep, on_trip = [], [], []
        for p in people:
            if p["name"] in trip_people_today:
                start_str = sheets_helper.minutes_to_time_str(trip_people_today[p["name"]])
                status = "responded" if reacted(p) else "no response"
                on_trip.append(f"{p['name']} - {start_str} ({status})")
                continue
            if reacted(p):
                awake.append(p["name"])
            else:
                asleep.append(p["name"])
        return awake, asleep, on_trip

    awake, asleep, on_trip = evaluate(roster)

    def section(title, items):
        if not items:
            return f"**{title}:** (none)"
        return f"**{title}:**\n" + "\n".join(f"- {i}" for i in items)

    asleep_section = section("Asleep", asleep)
    if asleep and supervisor:
        sup_mentions = []
        for p in supervisor:
            uid = username_map.get(p["discord_username"].lower())
            sup_mentions.append(f"<@{uid}>" if uid else f"@{p['discord_username']}")
        asleep_section += "\n\n" + " ".join(sup_mentions) + " - please check on them!"

    lines = [
        f"**{cfg['summary_header']}** ({today.strftime('%A %m/%d')})",
        section("Awake", awake),
        asleep_section,
        section("On Trip", on_trip),
    ]

    if owner:
        lines.append("**Also expected at meeting:** " + ", ".join(p["name"] for p in owner))

    summary = "\n\n".join(lines)

    discord_helper.post_message(bot_token, cfg["discord_channel_id"], summary)
    print("Posted summary message")


def _format_event_block(event, mention_parts):
    lines = [f"**{event['event_name']}** ({event['time_text']})"]
    if mention_parts:
        lines.append(" ".join(mention_parts))
    if event["transportation"]:
        lines.append(f"Transportation: {event['transportation']}")
    if event["notes"]:
        lines.append(f"Notes: {event['notes']}")
    return "\n".join(lines)


def run_reminders(cfg, bot_token, config_sheet, trip_sheet, all_tracked, alias_map, today, dry_run=False):
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.datetime.now(tz)
    now_minutes = now.hour * 60 + now.minute

    schedule_date = sheets_helper.get_state(config_sheet, "schedule_date")

    if schedule_date != today.isoformat():
        schedule = sheets_helper.get_day_schedule(trip_sheet, today, alias_map)
        sheets_helper.set_state(config_sheet, "schedule_date", today.isoformat())
        sheets_helper.set_state(config_sheet, "today_schedule", json.dumps(schedule))
        sheets_helper.set_state(config_sheet, "reminded_events", json.dumps([]))
        print(f"Built today's schedule: {len(schedule)} event(s) with tracked people.")
    else:
        schedule = json.loads(sheets_helper.get_state(config_sheet, "today_schedule", "[]"))

    reminded = set(json.loads(sheets_helper.get_state(config_sheet, "reminded_events", "[]")))
    lead = cfg["reminder_lead_minutes"]

    name_to_person = {p["name"]: p for p in all_tracked}

    username_map = {}
    if not dry_run:
        username_map = discord_helper.build_username_to_id_map(bot_token, cfg["discord_guild_id"])

    # Find everything due this run
    due = []
    for event in schedule:
        event_id = f"{event['event_name']}|{event['start_minutes']}"
        if event_id in reminded:
            continue

        minutes_until = event["start_minutes"] - now_minutes
        # Fire once the event is within `lead` minutes out, but not if it's
        # already started (covers the case a run was missed).
        if minutes_until > lead or minutes_until < 0:
            continue

        people = [name_to_person[n] for n in event["people"] if n in name_to_person]
        due.append((event_id, event, minutes_until, people))

    if not due:
        print("No new reminders to send this run.")
        return

    if dry_run:
        for event_id, event, minutes_until, people in due:
            print(f"=== DRY RUN: would remind for '{event['event_name']}' "
                  f"({event['time_text']}, starts in ~{minutes_until} min) ===")
            print(f"  Transportation: {event['transportation'] or '(none)'}")
            print(f"  Notes: {event['notes'] or '(none)'}")
            print(f"  People: {', '.join(p['name'] for p in people)}")
        reminded.update(event_id for event_id, _, _, _ in due)
        sheets_helper.set_state(config_sheet, "reminded_events", json.dumps(sorted(reminded)))
        return

    # Group events that start at the same time into one combined channel message
    groups = {}
    for event_id, event, minutes_until, people in due:
        groups.setdefault(event["start_minutes"], []).append((event_id, event, minutes_until, people))

    MAX_LEN = 1800

    for start_minutes, items in sorted(groups.items()):
        minutes_until = items[0][2]
        time_label = sheets_helper.minutes_to_time_str(start_minutes)
        header = f"\u23f0 Trips starting around {time_label} (~{minutes_until} min)"

        blocks = []
        for _event_id, event, _mu, people in items:
            mention_parts = []
            for p in people:
                uid = username_map.get(p["discord_username"].lower())
                mention_parts.append(f"<@{uid}>" if uid else f"@{p['discord_username']}")
            blocks.append(_format_event_block(event, mention_parts))

        # Chunk into multiple messages if the combined text would be too long
        chunks = []
        current = [header]
        current_len = len(header)
        for block in blocks:
            if current_len + len(block) + 2 > MAX_LEN and len(current) > 1:
                chunks.append("\n\n".join(current))
                current = [header + " (cont.)"]
                current_len = len(current[0])
            current.append(block)
            current_len += len(block) + 2
        chunks.append("\n\n".join(current))

        for chunk in chunks:
            try:
                discord_helper.post_message(bot_token, cfg["reminder_channel_id"], chunk)
                print(f"Posted reminder for trips at {time_label}")
            except Exception as exc:
                print(f"Failed to post channel reminder for trips at {time_label}: {exc}")

        # DMs - one per person per event
        for _event_id, event, _mu, people in items:
            for p in people:
                uid = username_map.get(p["discord_username"].lower())
                if not uid:
                    print(f"  No Discord ID found for {p['name']} (@{p['discord_username']}) - skipping DM")
                    continue

                dm_msg = (
                    f"\u23f0 Reminder: **{event['event_name']}** starts in ~{minutes_until} min "
                    f"({event['time_text']})"
                )
                if event["transportation"]:
                    dm_msg += f"\nTransportation: {event['transportation']}"
                if event["notes"]:
                    dm_msg += f"\nNotes: {event['notes']}"

                try:
                    discord_helper.send_dm(bot_token, uid, dm_msg)
                except Exception as exc:
                    print(f"  Failed to DM {p['name']}: {exc}")

    reminded.update(event_id for event_id, _, _, _ in due)
    sheets_helper.set_state(config_sheet, "reminded_events", json.dumps(sorted(reminded)))


if __name__ == "__main__":
    main()
