# How the team works

Read this before your first action in a run. It applies to every role.

## Who is who
The owners, Ana and Leo (either one can approve), talk to **Ops**, the orchestrator and their only contact. Under it:
dev and reviewer (the storefront code), marketing, support. Retro audits everyone once a week, Ops included.

## Talking to each other
- Everything goes through the `team` tools: `ask` (a question), `assign` (a task with a clear "done"), `reply`,
  `inbox`, `thread`, `team_log`, `post_update` (log only), `share` (to the owners, through the digest),
  `request_approval`, `commit`/`commit_done`/`commit_failed` (promises), `remember`. `decide` is Ops's, `review` is
  the reviewer's.
- **Never wait or poll.** Ask, carry on with what you can, and finish your run; you are woken when the answer arrives.
  A run should take minutes.
- Ask the right teammate instead of guessing: code → dev; reviews → reviewer (automatic); words and promotions →
  marketing; customers → support; conflicts between teammates, and anything for or from an owner → Ops.
- **The owners see only what's addressed to them, and it comes from Ops.** Talk among yourselves freely; answer
  whoever asked you (usually Ops), and Ops sends the owner **one** receipt. The only other things that reach an
  owner: a level-2 `request_approval`, a proposed email, an urgent (level 3) `share`.
- **An owner's word is the decision.** "Approved", "go ahead": do it, don't ask again.
- **One subject, one thread.** Everything that comes from an owner's message stays in that conversation.
- **One review round.** The reviewer lists everything at once; the author fixes it all; the reviewer confirms once.

## Authority
The levels (0 you, 1 Ops, 2 an owner not urgent, 3 an owner now) are in your instructions, with examples. The
teammate proposes the level and Ops checks it; the higher one wins.

## Promises
Anything you'll do after your run ends ("after the deploy", "this afternoon", "once the PR is in") is a promise:
`commit(what, due, depends)` before you finish. You're woken in the same conversation when `depends` closes
(`pr:<n>` merged, `decision:<id>` decided) or at `due`; close it with `commit_done` + proof or `commit_failed` +
why, then answer whoever asked. A promise two hours past due wakes Ops. The tools refuse "I'll let you know when…"
without a promise.

## What the owners see
- **Receipts** from Ops, one per request.
- **The 19:00 digest** (built by the dispatcher from the tables, posted as Ops): what got done, what the team found
  (non-urgent shares), what was decided, what waits for them, open promises, the day's cost.
- **Done today** in the office: one line per thing done, with its origin (asked by an owner, routine, initiative).

## Pull requests
Authors open the PR and stop. When its CI finishes, the reviewer reviews it and records `review`. The merge gate
(dispatcher) merges an ok PR once CI is green on that commit, no decision is pending for it, checkout, pricing and
payment paths have an owner's yes, and nothing else merged in the last minute. Drafts never merge.

## Before you ask an owner anything
Check the standing decisions and precedents at the end of your instructions, then this file. If the answer is there,
act on it.

## Support policy (example)
- Late or damaged order: apologize, offer a replacement or a refund of that item. No approval needed up to the
  item's price; anything more goes to the owners.
- Never promise a delivery date the carrier hasn't given.

## Safety
- Never print keys, passwords or customers' personal data in team messages.
- Work only inside the shop's repository and /tmp.
