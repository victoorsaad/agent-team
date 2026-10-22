"""Smoke tests for the orchestrator features: the merge gate's decision, approvals (ambiguous ones and bare numbers),
decisions, promises, the digest, the single door, the mailer's link check, and a team.json without any of it still
working as before. No Claude, no network, no GitHub. Run: python3 -m unittest -v"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="agent-team-test-")
EXAMPLE = os.path.join(ROOT, "examples", "tea-shop", "team.json")
os.environ.update({"TEAM_DATA_DIR": TMP, "TEAM_ENV_FILE": os.path.join(TMP, "none.env"), "TEAM_CONFIG": EXAMPLE,
                   "TEAM_WORKDIR": TMP, "TEAM_DRY_RUN": "1", "TEAM_DRY_RUN_SECONDS": "0.05"})
for k in ("TEAM_DISCORD_ENABLED", "TEAM_MAILER_ENABLED", "TEAM_GITHUB", "TEAM_OFFICE_TOKEN", "MAILER_CHECK_HOSTS"):
    os.environ.pop(k, None)

from agent_team import config, discord_bot, dispatcher, mailer, mcp_server, store  # noqa: E402


def fresh_db():
    path = os.path.join(TMP, "t%d.db" % (time.time() * 1e6))
    os.environ["TEAM_DB"] = path
    return store.db(path)


def as_member(me, tid=None):
    mcp_server.ME, mcp_server.CURRENT = me, tid


class UseConfig:
    """with UseConfig(path): config.team() is that team.json; the example comes back after."""
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        os.environ["TEAM_CONFIG"] = self.path
        config.reset()
        return config.team()

    def __exit__(self, *a):
        os.environ["TEAM_CONFIG"] = EXAMPLE
        config.reset()


def legacy_config():
    """The example as it was before the orchestrator: no single door, authority, digest, promises, gate or retro."""
    with open(EXAMPLE) as f:
        raw = json.load(f)
    for k in ("single_door", "authority", "digest", "promises", "merge_gate", "retro", "decision_kinds", "orchestrator"):
        raw.pop(k, None)
    raw["heartbeat"].pop("when")
    raw["roles"] = [r for r in raw["roles"] if r["id"] != "retro"]
    for r in raw["roles"]:
        r["prompt_file"] = os.path.join(os.path.dirname(EXAMPLE), r["prompt_file"])
    raw["playbook_file"] = os.path.join(os.path.dirname(EXAMPLE), raw["playbook_file"])
    path = os.path.join(TMP, "legacy.json")
    with open(path, "w") as f:
        json.dump(raw, f)
    return path


class LegacyConfigTest(unittest.TestCase):
    def test_without_orchestrator_works_as_before(self):
        with UseConfig(legacy_config()) as t:
            self.assertFalse(t.single_door)
            self.assertEqual(t.orchestrator, "ops")   # no "orchestrator" key: the first role, as before
            self.assertIsNone(t.digest)
            self.assertNotIn("# Authority: who decides what", dispatcher.system_prompt("dev"))
            self.assertNotIn("commit", dispatcher.run_prompt("dev"))
            as_member("support")
            c = fresh_db()
            names = {x["name"] for x in mcp_server.tools()}
            self.assertNotIn("review", names)
            self.assertNotIn("urgent", json.dumps([x for x in mcp_server.tools() if x["name"] == "share"]))
            # share goes straight to the humans (no digest), and "I'll let you know when" isn't refused
            mcp_server.call("share", {"text": "I'll let you know when the courier answers."})
            self.assertEqual(c.execute("SELECT kind, recipient FROM messages").fetchone()[:], ("update", "ana"))
            # request_approval still works without a kind; the human's answer goes to the agent who spoke last
            out = mcp_server.call("request_approval", {"what": "Refund order #1047 in full?"})
            self.assertIn("arrives in your inbox", out)
            tid = c.execute("SELECT thread FROM messages WHERE kind = 'approval'").fetchone()[0]
            discord_bot.answer(c, "ana", None, "yes", tid)
            self.assertEqual(c.execute("SELECT recipient FROM messages ORDER BY id DESC").fetchone()[0], "support")


class GateTest(unittest.TestCase):
    PR = {"state": "OPEN", "isDraft": False, "headRefOid": "abc", "mergeable": "MERGEABLE", "files": [{"path": "src/pages/a.tsx"}],
          "statusCheckRollup": [{"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}]}
    OK = {"head": "abc", "verdict": "ok", "at": 100.0}

    def gate(self, pr=None, verdict=None, decisions=(), **kw):
        return dispatcher.gate(dict(self.PR, **(pr or {})), dict(self.OK, **(verdict or {})), list(decisions),
                               config.team().gate_paths(), **kw)[0]

    def test_merges_only_when_everything_holds(self):
        self.assertEqual(self.gate(), "merge")
        self.assertEqual(self.gate({"headRefOid": "new"}), "drop")          # pushed again: reviewed again
        self.assertEqual(self.gate({"state": "MERGED"}), "drop")
        self.assertEqual(self.gate(verdict={"verdict": "block"}), "wait")
        self.assertEqual(self.gate({"isDraft": True}), "wait")
        self.assertEqual(self.gate({"mergeable": "CONFLICTING"}), "wait")
        self.assertEqual(self.gate({"statusCheckRollup": [{"status": "IN_PROGRESS"}]}), "wait")
        self.assertEqual(self.gate({"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]}), "wait")
        self.assertEqual(self.gate({"statusCheckRollup": []}), "wait")
        self.assertEqual(self.gate({"statusCheckRollup": []}, require_ci=False), "merge")

    def test_decisions_hold_it(self):
        self.assertEqual(self.gate(decisions=[{"id": 1, "status": "pending", "decided_at": None}]), "wait")
        self.assertEqual(self.gate(decisions=[{"id": 1, "status": "rejected", "decided_at": 200.0}]), "wait")
        self.assertEqual(self.gate(decisions=[{"id": 1, "status": "rejected", "decided_at": 50.0}]), "merge")   # before this review

    def test_sensitive_paths_need_a_human(self):
        pricing = {"files": [{"path": "src/lib/pricing.ts"}]}
        self.assertEqual(self.gate(pricing), "ask")
        self.assertEqual(self.gate(pricing, decisions=[{"id": 2, "status": "approved", "decided_at": 150.0}]), "merge")


class GateFlowTest(unittest.TestCase):
    def test_review_ask_decide_merge_receipt(self):
        c = fresh_db()
        tid = store.send(c, "ana", "ops", "task", "autumn bundle by Friday?")
        pr = dict(GateTest.PR, title="Autumn bundle", url="https://example.com/pull/48", files=[{"path": "src/lib/pricing.ts"}])
        merged = []

        def fake_gh(*args):
            if args[:2] == ("pr", "merge"):
                merged.append(args[2])
            return mock.Mock(returncode=0, stdout=json.dumps(pr), stderr="")

        os.environ["TEAM_GITHUB"] = "1"
        try:
            with mock.patch.object(mcp_server, "gh_pr", return_value=pr), mock.patch.object(dispatcher, "gh", side_effect=fake_gh):
                as_member("reviewer", tid)
                self.assertIn("review", {x["name"] for x in mcp_server.tools()})
                mcp_server.call("review", {"pr": 48, "verdict": "ok"})
                dispatcher.merge_gate(c)   # pricing.ts: the gate asks a human instead of merging
                self.assertEqual(merged, [])
                d = c.execute("SELECT * FROM decisions WHERE ref = 'pr:48'").fetchone()
                self.assertEqual((d["status"], d["owner"]), ("pending", "reviewer"))
                store.state(c, "gate_at", 0)
                dispatcher.merge_gate(c)   # still pending: no second question, no merge
                self.assertEqual(c.execute("SELECT count(*) FROM decisions").fetchone()[0], 1)
                store.decide(c, d["id"], "approved", "ana", sender="ops")
                store.state(c, "gate_at", 0)
                dispatcher.merge_gate(c)
                self.assertEqual(merged, ["48"])
                last = c.execute("SELECT * FROM messages ORDER BY id DESC").fetchone()
                self.assertEqual((last["sender"], last["recipient"], last["thread"]), ("ops", "ana", tid))
                self.assertIn("PR #48 merged", last["body"])
                self.assertIsNone(store.state(c, "verdict:48"))
        finally:
            os.environ.pop("TEAM_GITHUB")


class ApprovalTest(unittest.TestCase):
    def test_email_approval(self):
        pr = [{"id": 7, "kind": "pr"}]
        self.assertEqual(store.email_approval("approve", [], [3, 4]), ([3, 4], None))
        self.assertEqual(store.email_approval("approve 4", [], [3, 4]), ([4], None))
        self.assertEqual(store.email_approval("approve", [], []), (None, None))
        self.assertEqual(store.email_approval("ok, but change it", [], [3]), (None, None))
        ids, note = store.email_approval("approve", pr, [3, 4])   # a PR and emails pending: ambiguous
        self.assertIsNone(ids)
        self.assertIn("ambiguous", note)
        self.assertEqual(store.email_approval("approve the emails", pr, [3, 4]), ([3, 4], None))
        self.assertEqual(store.email_approval("aprovo os emails", pr, [3, 4]), ([3, 4], None))
        self.assertEqual(store.email_approval("approve 3 4", pr, [3, 4]), ([3, 4], None))   # bare numbers that are email ids
        self.assertEqual(store.email_approval("approve 48", pr, [3, 4]), (None, None))      # PR #48, not an email

    def test_discord_approves_only_unambiguous(self):
        c = fresh_db()
        tid = store.send(c, "ana", "ops", "task", "newsletter and the bundle")
        for to in ("a@example.com", "b@example.com"):
            c.execute("INSERT INTO outbox (agent, thread, to_email, subject, body, code, created_at) VALUES ('marketing', ?, ?, 's', 'b', 'x', ?)",
                      (tid, to, time.time()))
        store.open_decision(c, "reviewer", "pr", "PR #48 touches prices", thread=tid, ref="pr:48", mirror=False)
        warn, note = discord_bot.approve_emails(c, "approve", tid, "m1")
        self.assertIn("ambiguous", note)
        self.assertEqual(c.execute("SELECT count(*) FROM outbox WHERE status = 'pending'").fetchone()[0], 2)
        warn, note = discord_bot.approve_emails(c, "approve the emails", tid, "m2")
        self.assertIsNone(warn)
        self.assertIn("2 email(s) approved", note)
        self.assertEqual(c.execute("SELECT count(*) FROM outbox WHERE status = 'approved' AND approval_ref = 'm2'").fetchone()[0], 2)

    def test_single_door_routes_to_orchestrator(self):
        c = fresh_db()
        tid = store.send(c, "ana", "ops", "task", "bundle?")
        store.send(c, "reviewer", "ana", "approval", "merge PR #48?", thread=tid, mirror=False)
        discord_bot.answer(c, "ana", None, "approve", tid)
        self.assertEqual(c.execute("SELECT recipient FROM messages ORDER BY id DESC").fetchone()[0], "ops")
        discord_bot.answer(c, "ana", "marketing", "and the copy?", tid)   # a name still goes straight to it
        self.assertEqual(c.execute("SELECT recipient FROM messages ORDER BY id DESC").fetchone()[0], "marketing")


class DecisionTest(unittest.TestCase):
    def test_orchestrator_relays_only_after_a_human_spoke(self):
        c = fresh_db()
        tid = store.send(c, "ana", "ops", "task", "bundle?")
        as_member("reviewer", tid)
        out = mcp_server.call("request_approval", {"what": "PR #48 touches prices. Merge?", "kind": "pr", "pr": 48})
        self.assertIn("Decision 1", out)
        self.assertIn("already waiting", mcp_server.call("request_approval", {"what": "again", "kind": "pr", "pr": 48}))
        with self.assertRaises(ValueError):
            mcp_server.call("request_approval", {"what": "x", "kind": "pr"})   # kind pr needs the number
        self.assertNotIn("decide", {x["name"] for x in mcp_server.tools()})   # single door: only ops relays
        as_member("ops", tid)
        with self.assertRaises(ValueError):   # Ana hasn't answered since the decision opened
            mcp_server.call("decide", {"decision": 1, "verdict": "approved", "human": "ana"})
        store.send(c, "ana", "ops", "reply", "the PR, yes", thread=tid, mirror=False)
        mcp_server.call("decide", {"decision": 1, "verdict": "approved", "human": "ana"})
        d = c.execute("SELECT * FROM decisions WHERE id = 1").fetchone()
        self.assertEqual((d["status"], d["decided_by"]), ("approved", "ana"))
        self.assertIn("decision 1", c.execute("SELECT body FROM messages WHERE recipient = 'reviewer' ORDER BY id DESC").fetchone()[0])

    def test_human_decides_directly(self):
        c = fresh_db()
        did, _ = store.open_decision(c, "marketing", "spend", "Boost the post for $30?", mirror=False)
        as_member("leo")
        self.assertIn("decide", {x["name"] for x in mcp_server.tools()})
        mcp_server.call("decide", {"decision": did, "verdict": "rejected"})
        self.assertEqual(c.execute("SELECT status FROM decisions").fetchone()[0], "rejected")


class PromiseTest(unittest.TestCase):
    def test_due_at(self):
        now = time.mktime((2026, 1, 5, 10, 0, 0, 0, 0, -1))
        self.assertEqual(store.due_at("+90m", now), now + 5400)
        self.assertEqual(store.due_at("14:00", now), now + 4 * 3600)
        self.assertEqual(store.due_at("09:00", now), now + 23 * 3600)
        with self.assertRaises(ValueError):
            store.due_at("soon", now)

    def test_commit_wake_chase(self):
        c = fresh_db()
        tid = store.send(c, "ana", "ops", "task", "bundle by Friday?")
        as_member("dev", tid)
        with self.assertRaises(ValueError):   # promises something later without a promise
            mcp_server.call("ask", {"to": "ops", "question": "I'll let you know when the deploy is done, ok?"})
        out = mcp_server.call("commit", {"what": "Check the page after the deploy", "due": "+1h", "depends": "decision:1"})
        self.assertIn("Promise 1", out)
        mcp_server.call("ask", {"to": "ops", "question": "I'll let you know when the deploy is done, ok?"})   # now it's registered
        now = time.time()
        dispatcher.promises(c, now=now)   # not due, dependency unknown: nothing
        self.assertEqual(c.execute("SELECT count(*) FROM messages WHERE recipient = 'dev'").fetchone()[0], 0)
        dispatcher.promises(c, now=now + 3601)   # due: the agent is woken in its conversation
        wake = c.execute("SELECT * FROM messages WHERE recipient = 'dev'").fetchone()
        self.assertEqual((wake["sender"], wake["thread"]), ("system", tid))
        dispatcher.promises(c, now=now + 3601 + 2 * 3600 + 1)   # still open long after: the orchestrator is told, once
        dispatcher.promises(c, now=now + 3601 + 3 * 3600)
        self.assertEqual(c.execute("SELECT count(*) FROM messages WHERE recipient = 'ops' AND sender = 'system'").fetchone()[0], 1)
        out = mcp_server.call("commit_done", {"id": 1, "proof": "https://example.com/bundle"})
        self.assertIn("closed", out)
        self.assertEqual(c.execute("SELECT origin FROM done").fetchone()[0], "human")

    def test_decision_dependency_wakes(self):
        c = fresh_db()
        did, _ = store.open_decision(c, "marketing", "spend", "Boost?", mirror=False)
        as_member("marketing")
        mcp_server.call("commit", {"what": "Start the boost", "due": "+5h", "depends": "decision:%d" % did})
        store.decide(c, did, "approved", "ana")
        dispatcher.promises(c, now=time.time())
        self.assertIn("was approved", c.execute("SELECT body FROM messages WHERE recipient = 'marketing' AND sender = 'system'").fetchone()[0])


class DigestTest(unittest.TestCase):
    def test_shares_wait_for_the_digest(self):
        c = fresh_db()
        as_member("marketing")
        out = mcp_server.call("share", {"text": "Autumn teas are 40% of page views."})
        self.assertIn("digest", out)
        mcp_server.call("share", {"text": "Checkout is down!", "urgent": True})
        kinds = [r[0] for r in c.execute("SELECT kind FROM messages ORDER BY id")]
        self.assertEqual(kinds, ["share", "update"])

    def test_digest_text(self):
        c = fresh_db()
        self.assertIsNone(dispatcher.digest_text(c))   # a quiet day: nothing posted
        tid = store.send(c, "ana", "ops", "task", "bundle?")
        c.execute("INSERT INTO messages (thread, sender, recipient, kind, body, status, created_at) VALUES (?, 'marketing', 'ops', 'share', ?, 'done', ?)",
                  (tid, "Autumn teas are 40% of page views.", time.time()))
        store.open_decision(c, "marketing", "spend", "Boost the post for $30?", mirror=False)
        did, _ = store.open_decision(c, "reviewer", "pr", "PR #48 touches prices", ref="pr:48", mirror=False)
        store.decide(c, did, "approved", "ana")
        store.did(c, "reviewer", "PR #48 merged", thread=tid)
        c.execute("INSERT INTO promises (agent, what, due, created_at) VALUES ('dev', 'Check the page', ?, ?)", (time.time() - 60, time.time()))
        text = dispatcher.digest_text(c)
        for part in ("Done (1):", "Reviewer: PR #48 merged", "The team found:", "Autumn teas", "Decided today:", "✅",
                     "Waiting for you:", "Boost the post", "Open promises (1, 1 late)", "requests from you"):
            self.assertIn(part, text)
        self.assertNotIn("Ana approved", text.split("Decided today:")[0])   # humans' own decisions aren't "done by the team"


class MailerLinkTest(unittest.TestCase):
    def test_links_checked(self):
        c = fresh_db()
        os.environ["MAILER_CHECK_HOSTS"] = "shop.example.com"
        try:
            row = {"body": "See https://shop.example.com/b/autumn. and https://other.example.org/x", "subject": "Autumn", "to_email": "a@example.com"}
            self.assertEqual(mailer.links(row["body"]), {"https://shop.example.com/b/autumn"})
            with mock.patch.object(mailer, "link_exists", return_value=True):
                self.assertIsNone(mailer.links_ok(c, row))
            with mock.patch.object(mailer, "link_exists", return_value=False):
                self.assertIn("doesn't exist", mailer.links_ok(c, row))
            with mock.patch.object(mailer, "link_exists", side_effect=RuntimeError("link check: 503")):
                with self.assertRaises(RuntimeError):   # 5xx: not the email's fault, it stays approved
                    mailer.links_ok(c, row)
            c.execute("INSERT INTO outbox (agent, to_email, subject, body, code, status, created_at) VALUES "
                      "('marketing', 'a@example.com', 'Autumn', 'https://shop.example.com/b/autumn', 'x', 'sent', ?)", (time.time(),))
            reply = dict(row, subject="Re: Autumn", body="Here again: https://shop.example.com/b/winter")
            with mock.patch.object(mailer, "link_exists", return_value=True):
                self.assertIn("first email", mailer.links_ok(c, reply))
        finally:
            os.environ.pop("MAILER_CHECK_HOSTS")


class ConfigCheckTest(unittest.TestCase):
    def test_bad_new_keys(self):
        with open(EXAMPLE) as f:
            raw = json.load(f)
        for change in ({"merge_gate": {"sensitive_paths": ["("]}}, {"digest": {"at": "7pm"}}, {"retro": {"agent": "nobody"}},
                       {"heartbeat": dict(raw["heartbeat"], when="sometimes")}):
            bad = dict(raw, **change)
            for r in bad["roles"]:
                r.pop("prompt_file", None)
            path = os.path.join(TMP, "bad.json")
            with open(path, "w") as f:
                json.dump(bad, f)
            with self.assertRaises(config.ConfigError, msg=str(change)):
                config.Team(path)

    def test_retro_task(self):
        text = dispatcher.retro_task(dispatcher.dt.datetime(2026, 1, 5, 8, 30))
        self.assertIn("sqlite3 -readonly", text)
        self.assertIn("the authority levels", text)   # the locks it must never change
        self.assertIn("at most 3", text)


if __name__ == "__main__":
    unittest.main()
