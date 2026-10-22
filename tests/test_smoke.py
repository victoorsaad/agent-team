"""Smoke tests: the example team loads, the mailbox works, the MCP server answers over stdio, the dispatcher runs in
dry-run mode, the office serves its pages and the replay plays. No Claude, no network. Run: python3 -m unittest -v"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="agent-team-test-")
os.environ.update({"TEAM_DATA_DIR": TMP, "TEAM_DB": os.path.join(TMP, "team.db"), "TEAM_ENV_FILE": os.path.join(TMP, "none.env"),
                   "TEAM_CONFIG": os.path.join(ROOT, "examples", "tea-shop", "team.json"), "TEAM_WORKDIR": TMP,
                   "TEAM_DRY_RUN": "1", "TEAM_DRY_RUN_SECONDS": "0.05"})
for k in ("TEAM_DISCORD_ENABLED", "TEAM_MAILER_ENABLED", "TEAM_GITHUB", "TEAM_OFFICE_TOKEN"):
    os.environ.pop(k, None)

from agent_team import config, dispatcher, mailer, mcp_server, office, replay, store  # noqa: E402


def fresh_db():
    path = os.path.join(TMP, "t%d.db" % (time.time() * 1e6))
    os.environ["TEAM_DB"] = path
    return store.db(path)


class ConfigTest(unittest.TestCase):
    def test_example_loads(self):
        t = config.team()
        self.assertEqual(t.orchestrator, "lead")
        self.assertIn("ana", t.humans)
        self.assertTrue(t.role_prompt("dev"))
        self.assertIn("propose_email", dispatcher.system_prompt("dev"))

    def test_bad_config(self):
        bad = os.path.join(TMP, "bad.json")
        with open(bad, "w") as f:
            json.dump({"roles": [{"id": "a"}], "humans": [{"id": "a"}]}, f)
        with self.assertRaises(config.ConfigError):
            config.Team(bad)


class StoreTest(unittest.TestCase):
    def test_send_inbox_thread(self):
        c = fresh_db()
        m1 = store.send(c, "ana", "lead", "task", "hello")
        m2 = store.send(c, "lead", "dev", "task", "do it", thread=m1)
        self.assertEqual([r["id"] for r in store.inbox(c, "dev")], [m2])
        self.assertEqual(store.inbox(c, "dev"), [])
        self.assertEqual([r["id"] for r in store.thread(c, m1)], [m1, m2])
        with self.assertRaises(ValueError):
            store.send(c, "ana", "nobody", "task", "x")

    def test_humans_share_an_inbox(self):
        c = fresh_db()
        store.send(c, "support", "ana", "approval", "ok to refund?")
        self.assertEqual(len(store.inbox(c, "leo")), 1)

    def test_outgoing_refuses_secrets(self):
        env = os.path.join(TMP, ".env")
        with open(env, "w") as f:
            f.write("X=1")
        with self.assertRaises(ValueError):
            store.outgoing([env])
        ok = os.path.join(TMP, "report.csv")
        with open(ok, "w") as f:
            f.write("a,b")
        self.assertEqual(store.outgoing([ok]), [os.path.realpath(ok)])
        with self.assertRaises(ValueError):
            store.outgoing(["/etc/hosts"])

    def test_approve_regex(self):
        for yes in ("approve", "Aprovo", "ok!", "approve 3, 4", "LGTM"):
            self.assertTrue(store.APPROVE_RE.match(yes), yes)
        for no in ("ok, but change the subject", "not approved", "approve? not sure"):
            self.assertFalse(store.APPROVE_RE.match(no), no)


class McpTest(unittest.TestCase):
    def rpc(self, me, *msgs):
        env = dict(os.environ, PYTHONPATH=ROOT)
        p = subprocess.run([sys.executable, "-m", "agent_team", "mcp", "--as", me], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                           capture_output=True, text=True, env=env, cwd=ROOT, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return [json.loads(ln) for ln in p.stdout.splitlines()]

    def test_stdio_roundtrip(self):
        fresh_db()
        out = self.rpc("lead",
                       {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                       {"jsonrpc": "2.0", "method": "notifications/initialized"},
                       {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                       {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "assign", "arguments": {"to": "dev", "task": "Add the bundle page"}}},
                       {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "share", "arguments": {"text": "x" * 700}}})
        self.assertEqual([o["id"] for o in out], [1, 2, 3, 4])
        names = {t["name"] for t in out[1]["result"]["tools"]}
        self.assertTrue({"ask", "assign", "reply", "inbox", "request_approval", "remember"} <= names)
        self.assertNotIn("propose_email", names)   # the mailer is off by default
        self.assertIn("Sent", out[2]["result"]["content"][0]["text"])
        self.assertTrue(out[3]["result"]["isError"])   # too long for a human
        self.assertEqual(store.db().execute("SELECT count(*) FROM messages WHERE recipient = 'dev'").fetchone()[0], 1)

    def test_remember(self):
        mcp_server.ME = "lead"
        os.environ["TEAM_MEMORY_FILE"] = os.path.join(TMP, "MEMORY.md")
        mcp_server.call("remember", {"decision": "Free shipping above 50", "who": "ana"})
        self.assertIn("Free shipping above 50", dispatcher.memory())


class DispatcherTest(unittest.TestCase):
    def test_dry_run_tick(self):
        c = fresh_db()
        m = store.send(c, "ana", "lead", "ask", "status?")
        dispatcher.tick(c)
        for th in list(dispatcher.running.values()):
            th.join(10)
        reply = c.execute("SELECT * FROM messages WHERE reply_to = ?", (m,)).fetchone()
        self.assertIsNotNone(reply)
        self.assertEqual(reply["recipient"], "ana")
        run = c.execute("SELECT * FROM runs WHERE agent = 'lead'").fetchone()
        self.assertEqual(run["ok"], 1)

    def test_command_and_mcp_config(self):
        fresh_db()
        cmd = dispatcher.command("dev", "go")
        self.assertIn("--append-system-prompt", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "default")
        with open(cmd[cmd.index("--mcp-config") + 1]) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["mcpServers"]["team"]["args"][-1], "dev")

    def test_describe_masks_secrets(self):
        line = dispatcher.describe({"name": "Bash", "input": {"command": "curl -H 'Authorization: Bearer abcdefabcdef' x"}})
        self.assertNotIn("abcdefabcdef", line)

    def test_feed_parsing(self):
        rss = '<?xml version="1.0"?><rss><channel><item><title>v2 released</title><link>https://example.com/v2</link></item></channel></rss>'
        self.assertEqual(dispatcher.text_lines(rss, "application/rss+xml"), ["v2 released (https://example.com/v2)"])

    def test_command_trigger(self):
        c = fresh_db()
        dispatcher.triggers(c)
        task = c.execute("SELECT * FROM messages WHERE recipient = 'support'").fetchone()
        self.assertIn("Order #1047", task["body"])
        store.state(c, "trigger:new-support-tickets:at", 0)
        dispatcher.triggers(c)   # same lines again: nothing new
        self.assertEqual(c.execute("SELECT count(*) FROM messages WHERE recipient = 'support'").fetchone()[0], 1)


class MailerTest(unittest.TestCase):
    def test_hmac_approval(self):
        c = fresh_db()
        os.environ["TEAM_APPROVAL_SECRET"] = "test-secret"
        code = store.email_code("a@example.com", "Hi", "Body")
        oid = c.execute("INSERT INTO outbox (agent, to_email, subject, body, code, created_at) VALUES ('support', 'a@example.com', 'Hi', 'Body', ?, ?)",
                        (code, time.time())).lastrowid
        row = c.execute("SELECT * FROM outbox WHERE id = ?", (oid,)).fetchone()
        self.assertEqual(mailer.verify(c, row), "no verifiable approval")
        c.execute("UPDATE outbox SET status = 'approved', approved_by = 'cli', approval_ref = 'forged' WHERE id = ?", (oid,))
        self.assertIn("signature", mailer.verify(c, c.execute("SELECT * FROM outbox WHERE id = ?", (oid,)).fetchone()))
        c.execute("UPDATE outbox SET approval_ref = ? WHERE id = ?", (store.approval_sig("test-secret", oid, code), oid))
        self.assertIsNone(mailer.verify(c, c.execute("SELECT * FROM outbox WHERE id = ?", (oid,)).fetchone()))
        c.execute("UPDATE outbox SET body = 'changed' WHERE id = ?", (oid,))
        self.assertIn("changed", mailer.verify(c, c.execute("SELECT * FROM outbox WHERE id = ?", (oid,)).fetchone()))
        os.environ.pop("TEAM_APPROVAL_SECRET")


class OfficeTest(unittest.TestCase):
    def test_serves_pages_and_replay(self):
        server = office.serve("127.0.0.1", 0, os.path.join(ROOT, "examples", "tea-shop", "replay.json"), speed=20)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            team = json.load(urllib.request.urlopen(base + "/api/team"))
            self.assertEqual(team["name"], "Fern & Kettle")
            self.assertNotIn("prompt_file", json.dumps(team))
            html = urllib.request.urlopen(base + "/").read().decode()
            self.assertIn("/api/team", html)
            time.sleep(1.5)   # ~30 s of script at 20x
            st = json.load(urllib.request.urlopen(base + "/api/state"))
            self.assertTrue(st["replay"])
            self.assertGreater(len(st["feed"]), 3)
            self.assertEqual({a["id"] for a in st["agents"]}, set(config.team().roles))
        finally:
            server.shutdown()
            server.server_close()

    def test_token_and_proxy_rules(self):
        os.environ["TEAM_OFFICE_TOKEN"] = "s3cret"
        os.environ["TEAM_OFFICE_REQUIRE_TOKEN"] = "1"
        server = office.serve("127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/api/state")
            self.assertEqual(e.exception.code, 403)
            r = urllib.request.urlopen(base + "/api/team?token=s3cret")
            self.assertIn("office_token=s3cret", r.headers.get("set-cookie"))
        finally:
            server.shutdown()
            server.server_close()
            os.environ.pop("TEAM_OFFICE_TOKEN")
            os.environ.pop("TEAM_OFFICE_REQUIRE_TOKEN")


class ReplayTest(unittest.TestCase):
    def test_script_members_exist(self):
        s = replay.load(os.path.join(ROOT, "examples", "tea-shop", "replay.json"))
        self.assertGreater(len(s["events"]), 10)


if __name__ == "__main__":
    unittest.main()
