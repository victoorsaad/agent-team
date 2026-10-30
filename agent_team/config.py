"""Settings (environment variables, optionally from a .env file) and the team definition (team.json).

Everything that changes from one team to another lives in team.json and the prompt files next to it; everything that
changes from one machine to another (paths, ports, tokens) lives in the environment. See .env.example."""
import json
import os
import re

PKG = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PKG)
DEFAULT_CONFIG = os.path.join(ROOT, "examples", "tea-shop", "team.json")


def _read_env_file(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


_FILE = _read_env_file(os.environ.get("TEAM_ENV_FILE", os.path.join(os.getcwd(), ".env")))


def setting(key, default=""):
    """The process environment wins over the .env file. The MCP server runs under each agent's Claude with only the
    environment it was given, so it reads the same file."""
    v = os.environ.get(key)
    if v is None or v == "":
        v = _FILE.get(key)
    return default if v is None or v == "" else v


def flag(key, default=False):
    return str(setting(key, "1" if default else "0")).strip().lower() in ("1", "true", "yes", "on")


def number(key, default):
    try:
        return int(setting(key, str(default)))
    except ValueError:
        return default


def data_dir():
    return os.path.abspath(setting("TEAM_DATA_DIR", os.path.join(os.getcwd(), "data")))


def db_path():
    return os.path.abspath(setting("TEAM_DB", os.path.join(data_dir(), "team.db")))


def files_dir():
    return os.path.abspath(setting("TEAM_FILES", os.path.join(data_dir(), "files")))


def workdir():
    """Where agents run (the repository or folder they work on)."""
    return os.path.abspath(setting("TEAM_WORKDIR", os.getcwd()))


class ConfigError(ValueError):
    pass


class Team:
    """The team: roles (agents), humans, who approves what, routines and triggers. Optional, each off unless its key is
    in team.json: single_door (every human message goes to the orchestrator), authority (levels 0-3), digest (non-urgent
    shares batched into one daily message), promises, merge_gate (the dispatcher merges what the reviewer passed) and
    retro (a weekly audit of how the team worked)."""

    def __init__(self, path=None):
        self.path = os.path.abspath(path or setting("TEAM_CONFIG", DEFAULT_CONFIG))
        self.base = os.path.dirname(self.path)
        with open(self.path) as f:
            raw = json.load(f)
        self.raw = raw
        self.name = raw.get("name", "Agent team")
        self.language = raw.get("language", "en")
        self.description = raw.get("description", "")
        self.roles = {r["id"]: r for r in raw.get("roles", [])}
        self.humans = {h["id"]: h for h in raw.get("humans", [])}
        self.orchestrator = raw.get("orchestrator") or next(iter(self.roles), None)
        self.reviewer = raw.get("reviewer")
        self.human_max = int(raw.get("human_message_max", 600))
        self.approvals = raw.get("approvals", {})
        self.heartbeat = raw.get("heartbeat")
        self.routines = raw.get("routines", [])
        self.triggers = raw.get("triggers", [])
        self.office = raw.get("office", {})
        self.single_door = bool(raw.get("single_door"))
        self.authority = raw.get("authority") or {}
        self.digest = raw.get("digest")
        self.promises = raw.get("promises") or {}
        self.merge_gate = raw.get("merge_gate")
        self.retro = raw.get("retro")
        self.decision_kinds = raw.get("decision_kinds") or ["pr", "spend", "legal", "other"]
        # v0.3, each optional
        self.chat = raw.get("chat") or {}
        self.knowledge = raw.get("knowledge") or {}
        self.models = raw.get("models") or {}
        self.review = raw.get("review") or {}
        self.prompt_eval = raw.get("prompt_eval")
        self.deploy = raw.get("deploy")
        self.conflicts = raw.get("conflicts")
        self.north = raw.get("north")
        self.alerts = raw.get("alerts") or {}
        self.stand_ins = raw.get("stand_ins") or {}
        self.jobs_off = set(raw.get("jobs_off") or [])
        self.backup = raw.get("backup", {"keep": 7})
        self.validate()

    # ---- lookups -------------------------------------------------------------------------------------------------
    @property
    def members(self):
        """Everyone who can receive a message: agents first, then humans."""
        return list(self.roles) + list(self.humans)

    def who(self, member):
        """The member a message is really for: itself, or the teammate that stands in for a role this team doesn't have
        (stand_ins: {"designer": "dev"}). Lets shared prompts and routines name roles a smaller team folds together."""
        return self.stand_ins.get(member, member)

    def chat_provider(self):
        """"discord", "slack" or None. team.json chat.provider wins; a v0.2 install with TEAM_DISCORD_ENABLED=1 and no
        chat block keeps Discord."""
        if flag("TEAM_EVAL"):   # a prompt eval never reaches the chat
            return None
        p = self.chat.get("provider")
        if p:
            return None if p == "none" else p
        return "discord" if flag("TEAM_DISCORD_ENABLED") else None

    def job_on(self, name):
        return name not in self.jobs_off

    @property
    def primary_human(self):
        return next(iter(self.humans), None)

    def is_human(self, who):
        return who in self.humans

    def display(self, who):
        if who in self.roles:
            return self.roles[who].get("name", who)
        if who in self.humans:
            return self.humans[who].get("name", who)
        return {"system": self.name}.get(who, who)

    def summary(self, who):
        r = self.roles.get(who) or self.humans.get(who) or {}
        return r.get("summary", "")

    def read(self, rel):
        """A file next to team.json (prompts, playbook). Empty when missing."""
        if not rel:
            return ""
        p = rel if os.path.isabs(rel) else os.path.join(self.base, rel)
        try:
            with open(p) as f:
                return f.read()
        except OSError:
            return ""

    def role_prompt(self, agent):
        r = self.roles[agent]
        return self.read(r.get("prompt_file")) or r.get("prompt", "")

    def playbook(self):
        return self.read(self.raw.get("playbook_file"))

    def approval_rules(self):
        rules = self.approvals.get("rules") or []
        return "\n".join("- " + r for r in rules)

    def approvers(self):
        return self.approvals.get("approvers") or list(self.humans)

    def authority_table(self):
        """team.json → authority.levels as a markdown table for the prompts. Empty when there's no authority block."""
        levels = self.authority.get("levels") or []
        if not levels:
            return ""
        rows = ["| Level | Who decides | How the humans hear of it | Examples |", "|---|---|---|---|"]
        for lv in levels:
            rows.append("| %s | %s | %s | %s |" % (lv.get("level", ""), lv.get("decides", ""), lv.get("heard", ""),
                                                   "; ".join(lv.get("examples") or [])))
        rules = "\n".join("- " + r for r in self.authority.get("rules") or [])
        return "\n".join(rows) + ("\n\n" + rules if rules else "")

    def locks(self):
        """What no agent changes, the retro included, without a human: authority.locks."""
        return self.authority.get("locks") or ["the authority levels", "send limits", "access to production", "money rules"]

    def gate_paths(self):
        """merge_gate.sensitive_paths as one regex (None when there are none): a PR touching them needs a human's yes."""
        pats = (self.merge_gate or {}).get("sensitive_paths") or []
        return re.compile("|".join("(?:%s)" % p for p in pats), re.I) if pats else None

    def at(self, block):
        """(hour, minute) of a block's "at" ("19:00")."""
        h, m = (int(x) for x in (block or {}).get("at", "19:00").split(":"))
        return h, m

    def public(self):
        """What the office screen needs (no prompts)."""
        o = self.office
        return {
            "name": self.name, "language": self.language, "title": o.get("title") or self.name,
            "back": o.get("back"), "sign": o.get("sign"),
            "agents": [{"id": k, "name": v.get("name", k), "summary": v.get("summary", ""), "room": v.get("room"),
                        "color": v.get("color")} for k, v in self.roles.items()],
            "humans": [{"id": k, "name": v.get("name", k), "summary": v.get("summary", ""), "color": v.get("color")}
                       for k, v in self.humans.items()],
            "rooms": self.office.get("rooms", {}),
        }

    # ---- checks --------------------------------------------------------------------------------------------------
    def validate(self):
        if not self.roles:
            raise ConfigError("team.json has no roles")
        if not self.humans:
            raise ConfigError("team.json needs at least one human (someone has to approve things)")
        both = set(self.roles) & set(self.humans)
        if both:
            raise ConfigError("ids used for both a role and a human: %s" % ", ".join(sorted(both)))
        if "system" in self.roles or "system" in self.humans:
            raise ConfigError("'system' is reserved")
        if self.orchestrator not in self.roles:
            raise ConfigError("orchestrator %r is not a role" % self.orchestrator)
        if self.reviewer and self.reviewer not in self.roles:
            raise ConfigError("reviewer %r is not a role" % self.reviewer)
        if self.single_door and not self.raw.get("orchestrator"):
            raise ConfigError("single_door needs an explicit orchestrator")
        if self.merge_gate is not None and not self.reviewer:
            raise ConfigError("merge_gate needs a reviewer (the gate merges what the reviewer passed)")
        if self.retro and self.retro.get("agent") not in self.roles:
            raise ConfigError("retro points to unknown agent %r" % self.retro.get("agent"))
        for key, block in (("digest", self.digest), ("retro", self.retro)):
            if block and not re.fullmatch(r"\d{1,2}:\d{2}", str(block.get("at", "19:00"))):
                raise ConfigError("%s.at must look like \"19:00\"" % key)
        for p in (self.merge_gate or {}).get("sensitive_paths") or []:
            try:
                re.compile(p)
            except re.error as e:
                raise ConfigError("merge_gate.sensitive_paths: bad pattern %r (%s)" % (p, e))
        if self.heartbeat and self.heartbeat.get("when", "always") not in ("always", "backlog"):
            raise ConfigError("heartbeat.when must be 'always' or 'backlog'")
        prov = self.chat.get("provider")
        if prov and prov not in ("discord", "slack", "none"):
            raise ConfigError("chat.provider must be 'discord', 'slack' or 'none'")
        kp = self.knowledge.get("provider")
        if kp and kp not in ("markdown", "notion"):
            raise ConfigError("knowledge.provider must be 'markdown' or 'notion'")
        if kp == "markdown" and not (self.knowledge.get("path") or setting("TEAM_KNOWLEDGE_PATH")):
            raise ConfigError("knowledge (markdown) needs a path (team.json knowledge.path or TEAM_KNOWLEDGE_PATH)")
        for k, v in self.stand_ins.items():
            if v not in self.roles:
                raise ConfigError("stand_ins: %r stands in for %r but isn't a role" % (v, k))
        if self.north and self.north.get("agent", self.orchestrator) not in self.roles:
            raise ConfigError("north points to unknown agent %r" % self.north.get("agent"))
        if self.prompt_eval is not None and self.merge_gate is None:
            raise ConfigError("prompt_eval runs inside the merge gate: add merge_gate")
        for key, block in (("prompt_eval", self.prompt_eval), ("review", self.review), ("deploy", self.deploy)):
            for field in ("paths", "light_paths", "locked", "skip_paths"):
                pat = (block or {}).get(field)
                if pat:
                    try:
                        re.compile(pat)
                    except re.error as e:
                        raise ConfigError("%s.%s: bad pattern %r (%s)" % (key, field, pat, e))
        if self.north:
            if not re.fullmatch(r"\d{1,2}:\d{2}", str(self.north.get("at", "09:15"))):
                raise ConfigError("north.at must look like \"09:15\"")
        for a in self.approvers():
            if a not in self.humans:
                raise ConfigError("approver %r is not a human" % a)
        for r in self.routines + self.triggers + ([self.heartbeat] if self.heartbeat else []):
            if r.get("agent") not in self.roles:
                raise ConfigError("%r points to unknown agent %r" % (r.get("name", "heartbeat"), r.get("agent")))
        for t in self.triggers:
            if t.get("kind") not in ("feed", "command"):
                raise ConfigError("trigger %r: kind must be 'feed' or 'command'" % t.get("name"))
        for k, r in self.roles.items():
            if r.get("prompt_file") and not self.read(r["prompt_file"]):
                raise ConfigError("role %r: prompt file %s not found" % (k, r["prompt_file"]))


_TEAM = None


def team():
    global _TEAM
    if _TEAM is None:
        _TEAM = Team()
    return _TEAM


def reset():
    """Tests: forget the loaded team (after changing TEAM_CONFIG)."""
    global _TEAM
    _TEAM = None
