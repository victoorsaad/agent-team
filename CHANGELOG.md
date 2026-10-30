# Changelog

## 0.3.0

Integrations became pluggable, and the work now ends in production instead of at the merge. Every new block is off
until its key is in team.json; a v0.2 team.json runs unchanged (TEAM_DISCORD_ENABLED=1 without a `chat` block still
means Discord).

- **Chat providers**: `chat.provider` `discord` or `slack` (new). Shared inbound rules (`chat/inbound.py`); Slack bot
  over Socket Mode or the signed Events API; the mailer verifies chat approvals through the provider. Humans are
  @mentioned when an agent asks or answers them; `share` with `urgent` pings too.
- **Loose approvals**: a bare "approve" said where nothing is pending goes to the only decision waiting in the team,
  in that decision's conversation; never when two wait or an email does.
- **Knowledge providers**: `markdown` (any notes folder, Obsidian vaults included; optional git commit + push of the
  daily note) and `notion` (REST API). Context pages in every agent's instructions, `notes_search` / `notes_read`
  tools, and a daily note built from the mailbox (`agent-team daily-note`).
- **Models per run** (`models`): strong model for human conversations, cheaper one for light reviews; recorded per run.
- **Review rounds and overlap** (`review`): capped rounds, later rounds look only at what changed, duplicate and
  fix-of-a-fix PRs flagged, a review guide named in the task.
- **Prompt evals** (`prompt_eval`, `agent-team eval`): PRs that change prompts run real cases on the PR and the base
  before the gate merges; incident cases are scored; changed lines touching locked rules need a human.
  `retro.self_merge` lets the retro's PRs merge through the reviewer and the eval.
- **The week's north** (`north`, `set_north`): goals every run sees; self-started work must serve one or stop.
- **Deploy train and post-deploy verification** (`deploy`): batched deploys, checks after each deploy with automatic
  revert PRs, a down site @mentions the humans; promises on `pr:<n>` close when it's deployed.
- **Conflicts** (`conflicts`): rebase tasks to the PR's author, CHANGELOG-only conflicts resolved by the dispatcher,
  stale conflicting PRs closed.
- **Alerts** (`alerts`, `agent-team alert`): a monitor's post becomes a task for the orchestrator, answered in its thread.
- **Stand-ins** (`stand_ins`) and `jobs_off`: smaller teams reuse shared prompts and routines; several teams per host
  with the `agent-team-dispatcher@` / `agent-team-office@` units.
- **Office per team** (`office.title`, `back`, `prefix`, `css`).
- **Daily mailbox backup** (`backup.keep`, `agent-team backup`), and tests refuse to open a database outside the temp dir.
- Moved: `agent_team/discord_bot.py` → `agent_team/chat/discord_bot.py` (`agent-team discord-bot` unchanged).

## 0.2.0

The optional orchestrator: single door, authority levels, decisions, promises, merge gate, digest, retro.

## 0.1.0

First version: roles in team.json, the mailbox over MCP, the dispatcher, routines and triggers, the office, Discord and
the mailer.
