"""Tests for v0.3: chat providers (Discord, Slack) and the shared inbound rules, knowledge providers (Markdown, Notion)
and the daily note, models per run, the north, review rounds, stand-ins, alerts, the deploy train and post-deploy
helpers, conflicts, prompt evals, backups, the real-database guard and the office per team. No Claude, no network:
every API is a fake. Run: python3 -m unittest -v"""
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# The other test modules set the same variables at import; reuse theirs so whichever loads last doesn't break the others.
TMP = os.environ.get("TEAM_DATA_DIR") or tempfile.mkdtemp(prefix="agent-team-v03-")
EXAMPLE = os.path.join(ROOT, "examples", "tea-shop", "team.json")
for k, v in {"TEAM_DATA_DIR": TMP, "TEAM_ENV_FILE": os.path.join(TMP, "none.env"), "TEAM_CONFIG": EXAMPLE,
             "TEAM_WORKDIR": TMP, "TEAM_DRY_RUN": "1", "TEAM_DRY_RUN_SECONDS": "0.05"}.items():
    os.environ.setdefault(k, v)
for k in ("TEAM_DISCORD_ENABLED", "TEAM_MAILER_ENABLED", "TEAM_GITHUB", "TEAM_OFFICE_TOKEN", "SLACK_BOT_TOKEN", "NOTION_TOKEN"):
    os.environ.pop(k, None)

from agent_team import config, daily, dispatcher, evals, knowledge, mailer, mcp_server, notify, office, ship, store  # noqa: E402
from agent_team.chat import discord, inbound, slack, slack_bot  # noqa: E402
from agent_team.knowledge import notion  # noqa: E402


def fresh_db():
    path = os.path.join(TMP, "t%d.db" % (time.time() * 1e6))
    os.environ["TEAM_DB"] = path
    return store.db(path)


def as_member(me, tid=None):
    mcp_server.ME, mcp_server.CURRENT = me, tid


def team_with(**extra):
    """The example team.json plus `extra` keys, written to TMP; prompt paths made absolute."""
    with open(EXAMPLE) as f:
        raw = json.load(f)
    base = os.path.dirname(EXAMPLE)
    for r in raw["roles"]:
        r["prompt_file"] = os.path.join(base, r["prompt_file"])
    raw["playbook_file"] = os.path.join(base, raw["playbook_file"])
    for k, v in extra.items():
        if v is None:
            raw.pop(k, None)
        else:
            raw[k] = v
    path = os.path.join(TMP, "team-%d.json" % (time.time() * 1e6))
    with open(path, "w") as f:
        json.dump(raw, f)
    return path


class Team:
    def __init__(self, **extra):
        self.path = team_with(**extra)

    def __enter__(self):
        os.environ["TEAM_CONFIG"] = self.path
        config.reset()
        try:
            return config.team()
        except Exception:
            self.__exit__()
            raise

    def __exit__(self, *a):
        os.environ["TEAM_CONFIG"] = EXAMPLE
        config.reset()


class Env:
    def __init__(self, **kv):
        self.kv, self.old = kv, {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.old[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


SLACK = {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL": "C0TEAM", "TEAM_SLACK_USERS": "U0ANA:ana,U0LEO:leo"}


class FakeSlack:
    """Records Web API calls; chat.postMessage returns increasing ts values."""
    def __init__(self, replies=None):
        self.calls, self.n, self.replies = [], 0, replies or []

    def __call__(self, method, payload=None, get=False, token=None):
        self.calls.append((method, payload))
        if method == "chat.postMessage":
            self.n += 1
            return {"ok": True, "ts": "1700000000.%06d" % self.n}
        if method == "conversations.replies":
            return {"ok": True, "messages": self.replies}
        return {"ok": True}

    def posted(self):
        return [p for m, p in self.calls if m == "chat.postMessage"]


# ---- chat ------------------------------------------------------------------------------------------------------------

class ChatProviderTest(unittest.TestCase):
    def test_provider_choice(self):
        with Team(chat=None) as t:
            self.assertIsNone(t.chat_provider())
            with Env(TEAM_DISCORD_ENABLED="1"):
                self.assertEqual(t.chat_provider(), "discord")   # a v0.2 install keeps working
        with Team(chat={"provider": "slack"}) as t:
            self.assertEqual(t.chat_provider(), "slack")
            with Env(TEAM_EVAL="1"):
                self.assertIsNone(t.chat_provider())   # a prompt eval never posts
        with self.assertRaises(config.ConfigError):
            with Team(chat={"provider": "teams"}):
                pass

    def test_pings_only_humans_asked_something(self):
        with Team(chat={"provider": "slack"}):
            self.assertEqual(notify.pings("ana", "ask"), ["ana"])
            self.assertEqual(notify.pings("ana", "update"), [])
            self.assertEqual(notify.pings("ana", "update", force=True), ["ana"])
            self.assertEqual(notify.pings("dev", "ask"), [])
        with Team(chat={"provider": "slack", "mention": False}):
            self.assertEqual(notify.pings("ana", "ask"), [])

    def test_discord_mention_allows_exactly_the_humans(self):
        with Env(TEAM_DISCORD_USERS="111:ana,222:leo,333:stranger"):
            text, allowed = discord.mention("hi", ["ana"])
            self.assertEqual(text, "<@111> hi")
            self.assertEqual(allowed, {"parse": [], "users": ["111"]})
            self.assertEqual(discord.user_ids(), {"ana": "111", "leo": "222"})

    def test_slack_say_posts_as_the_agent_in_its_thread(self):
        fake = FakeSlack()
        with Team(chat={"provider": "slack"}), Env(TEAM_SLACK_ICONS="dev::hammer:", **SLACK), mock.patch.object(slack, "call", fake):
            slack.say("PR is up <!channel>", who="Dev", thread_id="171.000", ping=["ana"])
            p = fake.posted()[0]
            self.assertEqual((p["username"], p["thread_ts"], p["icon_emoji"]), ("Dev", "171.000", ":hammer:"))
            self.assertTrue(p["text"].startswith("<@U0ANA> "))
            self.assertNotIn("<!channel>", p["text"])   # the agent's own text never pings the channel

    def test_store_send_mirrors_to_slack_with_one_thread_per_conversation(self):
        fake = FakeSlack()
        with Team(chat={"provider": "slack"}), Env(**SLACK), mock.patch.object(slack, "call", fake):
            c = fresh_db()
            tid = store.send(c, "ana", "ops", "ask", "can we ship the autumn bundle?", mirror=False)
            store.send(c, "ops", "ana", "reply", "yes, Friday", reply_to=tid)
            store.send(c, "ops", "ana", "update", "done", thread=tid)
            roots = [p for p in fake.posted() if "thread_ts" not in p]
            self.assertEqual(len(roots), 1)   # one root message = the thread
            self.assertEqual({p.get("thread_ts") for p in fake.posted() if "thread_ts" in p}, {roots and "1700000000.000001"})
            self.assertTrue(fake.posted()[1]["text"].startswith("<@U0ANA>"))   # a reply pings the human
            self.assertFalse(fake.posted()[2]["text"].startswith("<@"))       # a status update doesn't

    def test_slack_signature(self):
        body, now = b'{"type":"event_callback"}', 1_700_000_000
        sig = "v0=" + hmac.new(b"s3cret", b"v0:%d:" % now + body, hashlib.sha256).hexdigest()
        self.assertTrue(slack_bot.signed("s3cret", str(now), body, sig, now=now))
        self.assertFalse(slack_bot.signed("s3cret", str(now), body, sig, now=now + 301))   # replayed later
        self.assertFalse(slack_bot.signed("other", str(now), body, sig, now=now))
        self.assertFalse(slack_bot.signed("s3cret", None, body, sig, now=now))


class SlackBotTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSlack()
        self.ctx = [Team(chat={"provider": "slack"}, alerts={"names": ["Uptime"]}), Env(**SLACK), mock.patch.object(slack, "call", self.fake)]
        for x in self.ctx:
            x.__enter__()
        self.c = fresh_db()

    def tearDown(self):
        for x in reversed(self.ctx):
            x.__exit__(None, None, None)

    def ev(self, **kw):
        return dict({"type": "message", "channel": "C0TEAM", "user": "U0ANA", "ts": "100.1", "text": "hello"}, **kw)

    def test_new_message_starts_a_conversation_in_its_own_thread(self):
        out = slack_bot.process(self.ev(text="<@UBOT> marketing, what's the angle for Friday?"), self.c)
        self.assertTrue(out.startswith("conversation"))
        m = self.c.execute("SELECT * FROM messages").fetchone()
        self.assertEqual((m["sender"], m["recipient"], m["kind"]), ("ana", "marketing", "ask"))
        self.assertEqual(notify.thread_of(self.c, "100.1"), m["id"])
        self.assertEqual(slack_bot.process(self.ev(text="again"), self.c), "ignored: seen")   # Slack retried it

    def test_answer_in_thread_goes_to_the_orchestrator_with_single_door(self):
        slack_bot.process(self.ev(), self.c)
        tid = self.c.execute("SELECT id FROM messages").fetchone()[0]
        store.send(self.c, "ops", "ana", "ask", "Which colour?", thread=tid)
        slack_bot.process(self.ev(ts="100.2", thread_ts="100.1", text="green"), self.c)
        last = self.c.execute("SELECT * FROM messages ORDER BY id DESC").fetchone()
        self.assertEqual((last["sender"], last["recipient"], last["thread"]), ("ana", "ops", tid))

    def test_strangers_and_bots(self):
        self.assertEqual(slack_bot.process(self.ev(user="U0EVE", ts="9.1"), self.c), "ignored: unknown user")
        self.assertIn("U0EVE", self.fake.posted()[-1]["text"])
        self.assertEqual(slack_bot.process(self.ev(bot_id="B1", username="Some bot", ts="9.2"), self.c), "ignored: bot")
        self.assertEqual(slack_bot.process(self.ev(subtype="message_changed", ts="9.3"), self.c), "ignored: message_changed")
        self.assertEqual(slack_bot.process(self.ev(channel="C0OTHER", ts="9.4"), self.c), "ignored: not our channel")

    def test_monitor_alert_opens_a_task_and_the_reply_comes_back_to_the_thread(self):
        out = slack_bot.process(self.ev(subtype="bot_message", bot_id="B9", username="Uptime", ts="200.1", user=None,
                                        text="checkout.example.com is down"), self.c)
        self.assertEqual(out, "alert")
        task = self.c.execute("SELECT * FROM messages").fetchone()
        self.assertEqual((task["sender"], task["recipient"]), ("system", "ops"))
        self.assertTrue(store.alert_thread(self.c, task["id"]))
        as_member("ops", task["id"])
        res = mcp_server.call("reply", {"message_id": task["id"], "answer": "Hosting restarted; back up since 10:42."})
        self.assertIn("alert's thread", res)
        self.assertEqual(self.fake.posted()[-1]["thread_ts"], "200.1")

    def test_email_approval_is_verified_against_slack(self):
        slack_bot.process(self.ev(), self.c)
        tid = self.c.execute("SELECT id FROM messages").fetchone()[0]
        code = store.email_code("a@example.com", "Hi", "Body")
        oid = self.c.execute("INSERT INTO outbox (agent, thread, to_email, subject, body, code, created_at) VALUES "
                             "('marketing', ?, 'a@example.com', 'Hi', 'Body', ?, ?)", (tid, code, time.time())).lastrowid
        slack_bot.process(self.ev(ts="100.3", thread_ts="100.1", text="approve"), self.c)
        row = self.c.execute("SELECT * FROM outbox WHERE id = ?", (oid,)).fetchone()
        self.assertEqual((row["status"], row["approved_by"], row["approval_ref"]), ("approved", "slack", "100.3"))
        self.fake.replies = [{"ts": "100.2", "bot_id": "B0", "text": "Email to approve (no. %d, code %s)" % (oid, code)},
                             {"ts": "100.3", "user": "U0ANA", "text": "approve"}]
        self.assertIsNone(mailer.verify(self.c, row))
        self.fake.replies[1]["user"] = "U0EVE"
        self.assertIn("isn't a human", mailer.verify(self.c, row))
        self.fake.replies = [{"ts": "100.3", "user": "U0ANA", "text": "approve"}]
        self.assertIn("wasn't shown", mailer.verify(self.c, row))


class LooseApprovalTest(unittest.TestCase):
    def test_only_the_single_pending_decision(self):
        c = fresh_db()
        self.assertIsNone(inbound.loose_approval(c, "approve"))
        did, mid = store.open_decision(c, "dev", "pr", "PR #48 touches pricing", ref="pr:48")
        self.assertEqual(inbound.loose_approval(c, "approve")["id"], did)
        self.assertEqual(inbound.loose_approval(c, "Approve the PR")["id"], did)
        self.assertEqual(inbound.loose_approval(c, "approve PR #48")["id"], did)
        self.assertIsNone(inbound.loose_approval(c, "approve PR #49"))
        self.assertIsNone(inbound.loose_approval(c, "ok"))            # small talk, never an approval
        self.assertIsNone(inbound.loose_approval(c, "approve the emails"))
        self.assertIsNone(inbound.loose_approval(c, "approve", tid=mid))   # said in its own thread: the normal path
        store.open_decision(c, "marketing", "spend", "Ads budget")
        self.assertIsNone(inbound.loose_approval(c, "approve"))        # two decisions: ambiguous

    def test_handle_routes_it_to_the_decision_thread(self):
        c = fresh_db()
        did, mid = store.open_decision(c, "dev", "pr", "PR #48 touches pricing", ref="pr:48")
        replies, new = inbound.handle(c, "ana", "approve", "approve", None, "m1", "discord")
        self.assertIsNone(new)
        self.assertIn("only pending decision", replies[0])
        last = c.execute("SELECT * FROM messages ORDER BY id DESC").fetchone()
        self.assertEqual((last["sender"], last["recipient"], last["thread"]), ("ana", "ops", mid))


# ---- knowledge -------------------------------------------------------------------------------------------------------

class MarkdownKnowledgeTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="notes-", dir=TMP)
        os.makedirs(os.path.join(self.root, "Team"))
        os.makedirs(os.path.join(self.root, "Shop", "Suppliers"))
        with open(os.path.join(self.root, "Team", "Context.md"), "w") as f:
            f.write("# Context\nWe sell loose-leaf tea. Free shipping over 40.\n")
        with open(os.path.join(self.root, "Shop", "Suppliers", "Darjeeling estate.md"), "w") as f:
            f.write("Orders ship monthly. Minimum order 5 kg.\n")

    def test_read_search_and_confine(self):
        with Team(knowledge={"provider": "markdown", "path": self.root, "read": ["Team/Context"]}):
            self.assertEqual(knowledge.read("Team/Context.md")[0], "Team/Context.md")
            self.assertEqual(knowledge.read("[[Darjeeling estate]]")[0], "Shop/Suppliers/Darjeeling estate.md")
            self.assertEqual(knowledge.search("minimum order")[0]["ref"], "Shop/Suppliers/Darjeeling estate.md")
            self.assertEqual(knowledge.search("darjeeling")[0]["ref"], "Shop/Suppliers/Darjeeling estate.md")
            with self.assertRaises(ValueError):
                knowledge.read("../../etc/passwd")
            ctx = knowledge.context()
            self.assertIn("Free shipping over 40", ctx)
            self.assertIn("Free shipping over 40", dispatcher.system_prompt("support"))
            as_member("support")
            self.assertIn("notes_search", {x["name"] for x in mcp_server.tools()})
            self.assertIn("Minimum order", mcp_server.call("notes_read", {"ref": "Darjeeling estate"}))

    def test_daily_note_written(self):
        with Team(knowledge={"provider": "markdown", "path": self.root, "daily_dir": "Team/Daily", "header": "> Related: [[Context]] · {date}"}):
            c = fresh_db()
            store.did(c, "dev", "Fixed the cart total")
            day = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            rel = dispatcher.daily_note(c, day)
            with open(os.path.join(self.root, rel)) as f:
                text = f.read()
            self.assertIn("Fixed the cart total", text)
            self.assertIn("> Related: [[Context]]", text)

    def test_needs_a_path(self):
        with self.assertRaises(config.ConfigError):
            with Team(knowledge={"provider": "markdown"}):
                pass


class NotionKnowledgeTest(unittest.TestCase):
    def test_page_id(self):
        self.assertEqual(notion.page_id("https://www.notion.so/acme/Tea-Plan-0123456789abcdef0123456789abcdef?pvs=4"),
                         "0123456789abcdef0123456789abcdef")
        self.assertEqual(notion.page_id("01234567-89ab-cdef-0123-456789abcdef"), "0123456789abcdef0123456789abcdef")
        with self.assertRaises(ValueError):
            notion.page_id("not a page")

    def test_read_search_write(self):
        pid = "0123456789abcdef0123456789abcdef"
        calls = []

        def fake(method, path, payload=None):
            calls.append((method, path, payload))
            if path.startswith("pages/"):
                return {"properties": {"Name": {"type": "title", "title": [{"plain_text": "Goals"}]}}}
            if path.startswith("blocks/"):
                return {"results": [{"type": "heading_2", "id": "b1", "heading_2": {"rich_text": [{"plain_text": "This quarter"}]}},
                                    {"type": "bulleted_list_item", "id": "b2", "bulleted_list_item": {"rich_text": [{"plain_text": "200 orders"}]}},
                                    {"type": "to_do", "id": "b3", "to_do": {"checked": True, "rich_text": [{"plain_text": "new site"}]}}],
                        "has_more": False}
            if path == "search":
                return {"results": [{"id": "aaaa-bbbb", "properties": {"t": {"type": "title", "title": [{"plain_text": "Pricing"}]}}}]}
            if path == "pages" and method == "POST":
                return {"url": "https://www.notion.so/daily"}
            return {}
        with Team(knowledge={"provider": "notion", "read": [pid], "daily_parent": pid}), mock.patch.object(notion, "call", fake):
            title, text = knowledge.read(pid)
            self.assertEqual(title, "Goals")
            self.assertEqual(text, "## This quarter\n- 200 orders\n- [x] new site")
            self.assertIn("200 orders", knowledge.context())
            self.assertEqual(knowledge.search("pricing"), [{"ref": "aaaabbbb", "line": "Pricing"}])
            self.assertEqual(knowledge.write_daily(dt.date.today(), "Team · today", "## Done\n\n- one\n| a | b |\n|---|---|"), "https://www.notion.so/daily")
            body = calls[-1][2]
            self.assertEqual(body["parent"], {"page_id": pid})
            self.assertEqual([b["type"] for b in body["children"]], ["heading_2", "bulleted_list_item", "paragraph"])


class DailyNoteTest(unittest.TestCase):
    def test_note_from_the_tables(self):
        c = fresh_db()
        store.did(c, "dev", "Shipped the autumn bundle", proof="https://example.com/pull/48")
        did, _ = store.open_decision(c, "marketing", "spend", "Newsletter ad, 50/month")
        store.decide(c, did, "approved", "ana")
        c.execute("INSERT INTO runs (agent, started_at, cost_usd) VALUES ('dev', ?, 0.42)", (time.time(),))
        title, text = daily.note(c, dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0))
        self.assertIn("Fern & Kettle", title)
        self.assertIn("Shipped the autumn bundle", text)
        self.assertIn("**approved** (Ana)", text)
        self.assertIn("| Dev | 1 | 0.42 |", text)


# ---- dispatcher ------------------------------------------------------------------------------------------------------

class ModelsTest(unittest.TestCase):
    def test_pick_model(self):
        with Team(models={"human": "opus", "light_review": "haiku", "human_except": ["support"]}) as t:
            t.roles["dev"]["model"] = "sonnet"
            c = fresh_db()
            self.assertEqual(dispatcher.pick_model(c, "dev", True), "opus")
            self.assertEqual(dispatcher.pick_model(c, "dev", False), "sonnet")
            self.assertIsNone(dispatcher.pick_model(c, "support", True))
            store.send(c, "system", "reviewer", "task", dispatcher.LIGHT_TAG + " PR #3 ...")
            self.assertEqual(dispatcher.pick_model(c, "reviewer", False), "haiku")
            store.send(c, "system", "reviewer", "task", "PR #4 ...")
            self.assertIsNone(dispatcher.pick_model(c, "reviewer", False))
            self.assertIn("--model", dispatcher.command("dev", "x", "opus"))


class NorthTest(unittest.TestCase):
    def test_north_reaches_runs_and_gates_initiatives(self):
        with Team(north={"days": [0], "at": "09:15"}):
            c = fresh_db()
            as_member("ops")
            self.assertIn("set_north", {x["name"] for x in mcp_server.tools()})
            as_member("dev")
            self.assertNotIn("set_north", {x["name"] for x in mcp_server.tools()})
            as_member("ops")
            mcp_server.call("set_north", {"goals": ["50 orders this week"], "why": "last week 38", "until": "2099-01-01"})
            self.assertIn("50 orders this week", dispatcher.run_prompt("dev", c))
            dispatcher.proactive(c, "marketing", "Check the changelog feed")
            self.assertIn("off north", c.execute("SELECT body FROM messages ORDER BY id DESC").fetchone()[0])
            dispatcher.proactive(c, "dev", "New error in the logs", maintenance=True)
            self.assertNotIn("off north", c.execute("SELECT body FROM messages ORDER BY id DESC").fetchone()[0])

    def test_expired_north_is_gone(self):
        with Team(north={}):
            c = fresh_db()
            store.state(c, "north", json.dumps({"goals": ["x"], "until": "2000-01-01", "by": "ops"}))
            self.assertEqual(dispatcher.north(c)[0], "")


class StandInTest(unittest.TestCase):
    def test_messages_to_a_missing_role_reach_its_stand_in(self):
        with Team(stand_ins={"designer": "dev"}):
            c = fresh_db()
            mid = store.send(c, "ops", "designer", "task", "new banner")
            self.assertEqual(c.execute("SELECT recipient FROM messages WHERE id = ?", (mid,)).fetchone()[0], "dev")
        with self.assertRaises(config.ConfigError):
            with Team(stand_ins={"designer": "nobody"}):
                pass


class ReviewRoundsTest(unittest.TestCase):
    def test_rounds(self):
        c = fresh_db()
        with mock.patch.object(dispatcher, "fast_forward", return_value=True), mock.patch.object(dispatcher, "repo_slug", return_value="o/r"):
            self.assertEqual(dispatcher.review_round(c, 7, "a" * 40), "")           # round 1
            store.state(c, "lastverdict:7", "block")
            self.assertIn("Round 2", dispatcher.review_round(c, 7, "b" * 40))
            self.assertEqual(dispatcher.review_round(c, 7, "c" * 40), "")           # a rebase after no new block
            store.state(c, "lastverdict:7", "block")
            self.assertIn("the cap", dispatcher.review_round(c, 7, "d" * 40))


class ShipTest(unittest.TestCase):
    def test_union_keeps_both_sides(self):
        text = "# Changelog\n<<<<<<< HEAD\n## 2026-01-02\n- main's line\n=======\n## 2026-01-02\n- the PR's line\n>>>>>>> pr\nold\n"
        self.assertEqual(ship.union(text), "# Changelog\n## 2026-01-02\n- main's line\n- the PR's line\nold\n")

    def test_page_urls(self):
        pm = [[r"web/app/(?:(.*)/)?page\.tsx", r"/\1"]]
        self.assertEqual(ship.page_urls(["web/app/page.tsx", "web/app/(shop)/teas/page.tsx", "web/app/teas/[slug]/page.tsx",
                                         "README.md"], pm), ["/", "/teas"])

    def test_deploy_train_batches(self):
        with Team(deploy={"workflow": "deploy.yml", "every_min": 30}):
            c = fresh_db()
            ran = []

            def gh_json(*a):
                if a[:2] == ("run", "list"):
                    return [{"headSha": "old", "status": "completed", "conclusion": "success", "databaseId": 1}]
                return {"files": [{"filename": "src/cart.ts"}], "commits": [{"commit": {"message": "fix: cart (#9)"}}]}

            def gh(*a):
                ran.append(a)
                return mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(dispatcher, "gh_json", gh_json), mock.patch.object(dispatcher, "gh", gh), \
                    mock.patch.object(dispatcher, "repo_slug", return_value="o/r"), \
                    mock.patch.object(ship, "git", return_value=mock.Mock(stdout="new\trefs/heads/main", returncode=0)):
                ship.deploy_train(c)
                self.assertEqual(ran, [])                    # first undeployed merge: wait for the batch
                store.state(c, "train_at", 0)
                store.state(c, "deploy_now", 1)              # a human waits for it
                ship.deploy_train(c)
                self.assertEqual(ran[0][:2], ("workflow", "run"))

    def test_jobs_off(self):
        with Team(jobs_off=["deploy_train"]) as t:
            self.assertFalse(t.job_on("deploy_train"))
            self.assertTrue(t.job_on("post_deploy"))


class PromptEvalTest(unittest.TestCase):
    def test_checks(self):
        stream = "\n".join(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": n, "input": i}]}})
                           for n, i in (("mcp__team__request_approval", {"what": "Refund 80?"}), ("Bash", {"command": "git status"})))
        uses = list(evals.tool_uses(stream))
        self.assertTrue(evals.check({"tool": "request_approval", "match": "refund"}, uses))
        self.assertTrue(evals.check({"bash": "gh pr create", "absent": True}, uses))
        self.assertFalse(evals.check({"any": ["propose_email"]}, uses))
        self.assertTrue(evals.check({"any_match": {"tools": ["Bash"], "match": "git"}}, uses))

    def test_locked_lines(self):
        diff = ("+++ b/team/prompts/support.md\n+Refunds up to 30 need no approval.\n+Be friendly.\n"
                "+++ b/src/app.ts\n+const limit = 3;\n")
        with Team(merge_gate={}, prompt_eval={"paths": "^team/prompts/", "locked": "approval|limit"}) as t:
            with mock.patch.object(dispatcher, "gh", return_value=mock.Mock(stdout=diff, returncode=0)):
                self.assertEqual(dispatcher.locked_lines(5, t.prompt_eval), ["team/prompts/support.md: Refunds up to 30 need no approval."])

    def test_needs_the_gate(self):
        with self.assertRaises(config.ConfigError):
            with Team(merge_gate=None, prompt_eval={"paths": "x"}):
                pass


# ---- store, office --------------------------------------------------------------------------------------------------

class StoreTest(unittest.TestCase):
    def test_tests_never_open_a_real_database(self):
        with self.assertRaises(RuntimeError):
            store.db(os.path.join(os.path.expanduser("~"), "agent-team-real.db"))

    def test_backup_keeps_the_last_n(self):
        c = fresh_db()
        folder = os.path.join(TMP, "bk%d" % (time.time() * 1e6))
        os.makedirs(folder)
        for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
            open(os.path.join(folder, "team-%s.db" % d), "w").close()
        store.backup(c, folder, keep=2)
        self.assertEqual(len(os.listdir(folder)), 2)
        self.assertIn("team-%s.db" % time.strftime("%Y-%m-%d"), os.listdir(folder))

    def test_alert_from_the_cli_path(self):
        c = fresh_db()
        mid = store.open_alert(c, "disk 91% on the shop server")
        self.assertTrue(store.alert_thread(c, mid))
        self.assertIn("disk 91%", c.execute("SELECT body FROM messages WHERE id = ?", (mid,)).fetchone()[0])


class OfficePerTeamTest(unittest.TestCase):
    def test_title_css_and_prefix(self):
        with Team(office={"title": "Kettle HQ", "css": {"bg": "#101820", "bad": "red;}</style>"}, "prefix": "/office",
                          "back": {"href": "/", "label": "Back"}}) as t:
            html = office.page().decode()
            self.assertIn("<title>Kettle HQ</title>", html)
            self.assertIn("--bg:#101820;", html)
            self.assertNotIn("</style>\"", html)
            self.assertNotIn("--bad", html)
            self.assertEqual(t.public()["title"], "Kettle HQ")
            server = office.serve("127.0.0.1", 0)
            import threading
            import urllib.request
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                port = server.server_address[1]
                with urllib.request.urlopen("http://127.0.0.1:%d/office/api/team" % port) as r:
                    self.assertEqual(json.loads(r.read())["title"], "Kettle HQ")
                with urllib.request.urlopen("http://127.0.0.1:%d/office/" % port) as r:
                    self.assertIn(b"Kettle HQ", r.read())
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
