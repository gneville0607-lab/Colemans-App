"""
main.py
Entry point. Run with one argument: "morning" or "summary".

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


def load_json(filename):
    with open(os.path.join(os.path.dirname(__file__), filename)) as f:
        return json.load(f)


def parse_meeting_time_to_minutes(meeting_time_str):
    hour, minute = meeting_time_str.split(":")
    return int(hour) * 60 + int(minute)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("morning", "summary"):
        print("Usage: python main.py [morning|summary]")
        sys.exit(1)

    mode = sys.argv[1]
    cfg = load_json("config.json")
    roster_cfg = load_json("roster.json")
    dry_run = os.environ.get("DRY_RUN", "").lower() == "true"

    target_time = cfg["morning_time"] if mode == "morning" else cfg["summary_time"]
    if not dry_run and not in_time_window(target_time, cfg["timezone"], cfg["time_tolerance_minutes"]):
        print(f"Not within the {target_time} {cfg['timezone']} window - skipping this run.")
        return

    bot_token = os.environ["DISCORD_BOT_TOKEN"]
    creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")

    client = sheets_helper.get_client(creds_path)
    config_sheet = client.open_by_key(cfg["config_spreadsheet_id"])
    trip_sheet = client.open_by_key(cfg["field_trip_spreadsheet_id"])

    roster = roster_cfg["roster"]
    exempt = roster_cfg["exempt"]
    supervisor = roster_cfg["supervisor"]
    owner = roster_cfg["owner"]

    # All people whose names might appear on the field trip sheet
    all_tracked = roster + supervisor + exempt

    today = datetime.date.today()
    cutoff_minutes = parse_meeting_time_to_minutes(cfg["trip_cutoff_time"])

    alias_map = sheets_helper.build_alias_map(all_tracked)
    trip_people_today = sheets_helper.get_morning_trip_people(
        spreadsheet=trip_sheet,
        today=today,
        alias_map=alias_map,
        cutoff_time_minutes=cutoff_minutes,
    )

    if mode == "morning":
        run_morning(cfg, bot_token, config_sheet, roster, supervisor, trip_people_today, today, dry_run)
    else:
        run_summary(cfg, bot_token, config_sheet, roster, exempt, supervisor, owner, trip_people_today, today, dry_run)


def run_morning(cfg, bot_token, config_sheet, roster, supervisor, trip_people_today, today, dry_run=False):
    to_ping = roster + supervisor

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


def run_summary(cfg, bot_token, config_sheet, roster, exempt, supervisor, owner, trip_people_today, today, dry_run=False):
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
        print("Exempt:", ", ".join(p["name"] for p in exempt) or "(none)")
        print("Supervisor:", ", ".join(p["name"] for p in supervisor) or "(none)")
        print("Owner:", ", ".join(p["name"] for p in owner) or "(none)")
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
    sup_awake, sup_asleep, sup_on_trip = evaluate(supervisor)

    def section(title, items):
        if not items:
            return f"**{title}:** (none)"
        return f"**{title}:**\n" + "\n".join(f"- {i}" for i in items)

    lines = [
        f"**{cfg['summary_header']}** ({today.strftime('%A %m/%d')})",
        section("Awake", awake),
        section("Asleep", asleep),
        section("On Trip", on_trip),
        section("Exempt", [p["name"] for p in exempt]),
    ]

    if supervisor:
        sup_lines = []
        sup_lines += [f"{n} (awake)" for n in sup_awake]
        sup_lines += [f"{n} (asleep)" for n in sup_asleep]
        sup_lines += sup_on_trip
        lines.append(section("Supervisor", sup_lines))

    if owner:
        lines.append("**Also expected at meeting:** " + ", ".join(p["name"] for p in owner))

    summary = "\n\n".join(lines)

    discord_helper.post_message(bot_token, cfg["discord_channel_id"], summary)
    print("Posted summary message")


if __name__ == "__main__":
    main()
