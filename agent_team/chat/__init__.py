"""Chat providers: where the humans talk to the team. One per team, picked in team.json (`"chat": {"provider":
"discord" | "slack" | "none"}`); secrets come from the environment (see .env.example).

Every provider module has the same small surface, so the rest of the framework never knows which one runs:

- enabled() → bool: configured enough to post.
- say(text, who=None, thread_id=None, files=None, ping=()): post as `who` (an agent's display name), in a thread or the
  channel, @mentioning the humans in `ping`. Never raises: a chat outage must not break an agent's run.
- new_thread(title) → external id: a thread for a conversation that has none yet (Discord: a channel thread; Slack: a
  root message whose replies are the thread).
- user_ids() → {human id: chat user id}, from TEAM_DISCORD_USERS / TEAM_SLACK_USERS.
- verify_approval(external, ref, code) → None when the chat message `ref` in thread `external` is a listed human's
  "approve" posted after the email with `code` was shown; else why not. The mailer calls it before sending.

The bots that read the humans' messages are separate processes: `agent-team discord-bot` and `agent-team slack-bot`;
their shared logic (who gets a message, approvals) is in inbound.py."""
from .. import config


def provider():
    """The chat module this team uses, or None."""
    name = config.team().chat_provider()
    if name == "discord":
        from . import discord
        return discord
    if name == "slack":
        from . import slack
        return slack
    return None


def enabled():
    p = provider()
    return bool(p and p.enabled())


def parse_users(raw):
    """"<chat user id>:<human id>, …" → {human id: chat user id} (only humans in team.json)."""
    t = config.team()
    out = {}
    for pair in (raw or "").split(","):
        if ":" not in pair:
            continue
        uid, who = (x.strip() for x in pair.split(":", 1))
        if uid and who in t.humans:
            out[who] = uid
    return out
