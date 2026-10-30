#!/bin/bash
# Example install on a Linux server with systemd. Idempotent. Run as root from the repository:
#   sudo bash deploy/install.sh /opt/agent-team
# It creates three users: `agents` (runs the dispatcher, so every agent), `teambot` (Discord or Slack bot) and `mailer`
# (sends approved e-mail). Only the mailer and the bot can read their own secrets; agents can't.
# Review it before running: it's a starting point, not a hardened setup (see SECURITY.md).
set -euo pipefail
APP=${1:-/opt/agent-team}
DATA=/var/lib/agent-team
ETC=/etc/agent-team
SRC=$(cd "$(dirname "$0")/.." && pwd)

install -d -m 755 "$APP" && cp -r "$SRC/agent_team" "$SRC/examples" "$APP/"
for u in agents teambot mailer; do id "$u" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$u"; done
getent group agent-team >/dev/null || groupadd --system agent-team
for u in agents teambot mailer; do usermod -aG agent-team "$u"; done
install -d -m 2770 -o agents -g agent-team "$DATA"
install -d -m 750 -o root -g agent-team "$ETC"
install -d -m 750 -o root -g agent-team "$ETC/teams"   # one <name>.env per extra team (agent-team-dispatcher@<name>)
# Shared, non-secret settings (team.json path, data dir, caps). Readable by the group.
test -f "$ETC/env" || { install -m 640 -o root -g agent-team "$SRC/.env.example" "$ETC/env"; \
  sed -i "s|^TEAM_DATA_DIR=.*|TEAM_DATA_DIR=$DATA|; s|^TEAM_CONFIG=.*|TEAM_CONFIG=$APP/examples/tea-shop/team.json|" "$ETC/env"; }
# Secrets, one file per service user (fill them in by hand):
test -f "$ETC/dispatcher.env" || install -m 600 -o agents /dev/null "$ETC/dispatcher.env"   # CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY
test -f "$ETC/discord.env" || install -m 600 -o teambot /dev/null "$ETC/discord.env"       # DISCORD_BOT_TOKEN
test -f "$ETC/slack.env" || install -m 600 -o teambot /dev/null "$ETC/slack.env"           # SLACK_BOT_TOKEN, SLACK_APP_TOKEN or SLACK_SIGNING_SECRET
test -f "$ETC/mailer.env" || install -m 600 -o mailer /dev/null "$ETC/mailer.env"          # SMTP_*, TEAM_APPROVAL_SECRET

for unit in "$SRC"/deploy/systemd/*.service; do   # includes the @ templates for more teams on one host
  sed "s|@APP@|$APP|g; s|@ETC@|$ETC|g; s|@DATA@|$DATA|g" "$unit" > "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload
echo "Installed in $APP. Edit $ETC/env (TEAM_WORKDIR, TEAM_DRY_RUN=0 when ready), then:"
echo "  systemctl enable --now agent-team-dispatcher agent-team-office"
echo "  # optional: agent-team-discord or agent-team-slack, agent-team-mailer"
echo "  # another team on this host: $ETC/teams/<name>.env, then systemctl enable --now agent-team-dispatcher@<name> agent-team-office@<name>"
