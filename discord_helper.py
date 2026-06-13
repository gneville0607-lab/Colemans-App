"""
discord_helper.py
Thin wrapper around the Discord REST API (no gateway connection needed,
since this script runs as a short-lived scheduled job).

Note: build_username_to_id_map requires the "Server Members Intent"
toggle to be enabled on the bot in the Discord Developer Portal
(Bot tab -> Privileged Gateway Intents).
"""

import urllib.parse
import requests

API_BASE = "https://discord.com/api/v10"


def _headers(bot_token):
    return {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }


def post_message(bot_token, channel_id, content):
    """Posts a message and returns its message ID."""
    url = f"{API_BASE}/channels/{channel_id}/messages"
    resp = requests.post(url, headers=_headers(bot_token), json={"content": content})
    resp.raise_for_status()
    return resp.json()["id"]


def get_reaction_user_ids(bot_token, channel_id, message_id, emoji):
    """Returns a set of user ID strings who reacted with `emoji`."""
    encoded_emoji = urllib.parse.quote(emoji)
    user_ids = set()
    after = None

    while True:
        url = f"{API_BASE}/channels/{channel_id}/messages/{message_id}/reactions/{encoded_emoji}"
        params = {"limit": 100}
        if after:
            params["after"] = after

        resp = requests.get(url, headers=_headers(bot_token), params=params)
        if resp.status_code == 404:
            return user_ids
        resp.raise_for_status()

        users = resp.json()
        if not users:
            break

        for u in users:
            user_ids.add(str(u["id"]))

        if len(users) < 100:
            break
        after = users[-1]["id"]

    return user_ids


def build_username_to_id_map(bot_token, guild_id):
    """
    Fetches all members of the guild and returns a dict mapping
    lowercased Discord username -> user ID string.

    Requires the "Server Members Intent" to be enabled for the bot.
    """
    username_to_id = {}
    after = "0"

    while True:
        url = f"{API_BASE}/guilds/{guild_id}/members"
        params = {"limit": 1000, "after": after}
        resp = requests.get(url, headers=_headers(bot_token), params=params)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break

        for m in batch:
            user = m.get("user", {})
            username = user.get("username", "")
            user_id = user.get("id")
            if username and user_id:
                username_to_id[username.lower()] = str(user_id)

        if len(batch) < 1000:
            break
        after = batch[-1]["user"]["id"]

    return username_to_id
