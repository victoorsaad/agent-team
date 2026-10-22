# Security

agent-team runs AI agents unattended, with tools that can read and change files, run commands and reach the network.
Treat it like giving a contractor a laptop: decide what they can touch before they start.

## Threat model in one paragraph

Agents read untrusted text (web pages, customer messages, issues, files people attach). Any of it can contain
instructions aimed at the agent (prompt injection). Assume an agent can be talked into doing anything its tools allow.
The defenses below limit **what the tools allow** and put a human in front of anything that leaves your systems.

## What the framework does

- **Human approval for side effects.** The approval rules in `team.json` go into every agent's instructions, and the
  `request_approval` tool routes the question to a human. For e-mail, approval is enforced, not just requested: the
  mailer sends only emails approved with `agent-team approve <id>` (an HMAC signed with `TEAM_APPROVAL_SECRET`) or
  approved by a listed human in the Discord thread (re-checked against Discord's API), and only if the email is
  byte-for-byte what the human saw.
- **The merge gate (optional, `merge_gate` in team.json).** With it on, the reviewer only records a verdict and the
  dispatcher does the merge. What the code enforces, for every merge the dispatcher makes: the verdict is for the PR's
  current head commit (a new push needs a new review); CI finished and passed on it; no decision for that PR is pending,
  and none was rejected after the review; a PR touching `sensitive_paths` waits for a human's approval in `decisions`;
  no `deploy_workflow` run is in progress; one merge a minute. What it does **not** enforce: it can't stop an agent
  that holds a GitHub token with merge rights from running `gh pr merge` itself, and `decisions` lives in the mailbox,
  which agents can write (the `decide` tool checks that a human spoke in that conversation, but that is a check on the
  orchestrator, not a boundary). To make the gate a real boundary, give the agents' token no merge rights (or require
  a human review in branch protection) and let only the gate's identity merge.
- **Decisions and the orchestrator.** `request_approval` opens a row in `decisions`; with a single door only the
  orchestrator (and the humans, from the CLI or their own session) can record an answer, and only for an approver.
  With Discord, a human's "approve" in a thread where a PR or another decision is also pending approves no email
  unless it says so ("approve the emails") or names pending email ids; the mailer still re-checks the approval
  message on Discord before sending. These reduce mistakes; the e-mail check above is what is enforced.
- **Links in e-mail (optional, `MAILER_CHECK_HOSTS`).** The mailer refuses an email whose links to your own hosts
  answer 404/410, and a reply that links somewhere the first email didn't; a 5xx keeps the email for the next round.
- **Least privilege by default.** `TEAM_PERMISSION_MODE=default` plus a per-role `allowed_tools` list. Integrations
  (Discord, e-mail, GitHub) are off until you turn them on.
- **Budgets.** Few runs at once, a daily cap per agent, a daily cap on self-started work, a timeout per run, a pause
  when the usage limit is hit, and `agent-team stop` to halt everything.
- **No secrets in messages.** Files an agent attaches are checked (no dotfiles, `.env`, keys, credentials; only under
  the work dir, the team's files or `/tmp`). The office masks token-looking strings in what it shows.
- **The office** binds to `127.0.0.1`; remote access needs `TEAM_OFFICE_TOKEN` or an authenticating proxy.

## What you must do

1. **Sandbox the agents.** Run the dispatcher (and so every agent) as its own OS user, ideally in a container or VM,
   with only the repository it works on. If you use `bypassPermissions`, the sandbox is the only boundary.
2. **Keep secrets out of the agents' reach.** `TEAM_APPROVAL_SECRET`, SMTP and Discord credentials belong to the
   mailer/bot users, not to the agents' user. The mailbox (SQLite) is **not** an auth boundary: anything that can open
   the file can write to it, and an agent with a shell can start the MCP server as a human. That's why e-mail
   approvals are verified out of band; do the same for any new side effect you add.
3. **Give read-only access to data.** If agents need production data, give them a read-only role with no personal
   data or credentials in reach.
4. **Restrict the network** if you can (egress allow-list). An agent that can't reach a host can't leak to it.
5. **Review what they merge.** Require a human approval for PRs that touch money, auth or customer-facing screens:
   list those paths in `merge_gate.sensitive_paths`, and remove merge rights from the agents' GitHub token so the gate
   is the only way in.
6. **The retro reads the whole mailbox** (read-only), including proposed emails. Give that role only `sqlite3
   -readonly` and the repository, and keep its changes behind a human's approval (the example does both).

## Reporting a vulnerability

Please report security issues privately to the maintainers (GitHub security advisories on this repository) rather
than in a public issue. Include what you found, how to reproduce it and the impact. We'll answer as soon as we can.
