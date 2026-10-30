"""After the merge (team.json deploy and conflicts, TEAM_GITHUB=1): the work isn't done when the PR merges, it's done
when it works in production.

- Deploy train (deploy.workflow): main is deployed in batches, at most every deploy.every_min (30) after the first
  undeployed merge; at once for a revert or a PR a human is waiting for; never for skip_paths only (docs). One run at
  a time; a failed run isn't retried on the same commit (the orchestrator, or review.fallback_owner, hears of it).
  With deploy.on_merge the platform deploys each merge itself and there is no train.
- Post-deploy verification (deploy.verify): after_min (10) after each deploy, the site's paths and every page the
  deploy changed (page_map) must answer, and no new error shape may show up in logs_command's output. Broken: a revert
  PR is opened and marked ok for the merge gate (it only undoes what was live before), the author gets the evidence,
  and a site that's down @mentions the humans. New errors below revert_errors go to the author as a task.
- Conflicts (conflicts): a PR that conflicts with the base gets a rebase task for its author once per commit; when
  every conflict is in union_files (a CHANGELOG that every PR touches) the dispatcher rebases it itself, keeping both
  sides, runs `check` and pushes. close_after_h (24) later on the same commit the PR is closed, the branch kept."""
import datetime as dt
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import config, notify, store


def _d():
    from . import dispatcher
    return dispatcher


def git(*a, cwd=None, timeout=120):
    return subprocess.run(["git", "-C", cwd or config.workdir(), *a], capture_output=True, text=True, timeout=timeout)


def base_branch():
    t = config.team()
    return (t.deploy or {}).get("branch") or (t.merge_gate or {}).get("base") or "main"


def owner():
    return _d().fallback_owner()


def thread_of_pr(c, n):
    th = c.execute("SELECT thread FROM messages WHERE body LIKE ? ORDER BY id LIMIT 1", ("%%/pull/%d%%" % n,)).fetchone()
    return th["thread"] if th else None


def ts(v):
    return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()


# ---- the deploy train -----------------------------------------------------------------------------------------------

def contains(head, sha):
    """head has sha in its history (sha went out with head)."""
    try:
        return _d().gh_json("api", "repos/%s/compare/%s...%s" % (_d().repo_slug(), sha, head), "--jq", "{s: .status}")["s"] in ("ahead", "identical")
    except Exception:  # noqa: BLE001
        return False


def deploy_train(c):
    t = config.team()
    d = t.deploy or {}
    if not d.get("workflow") or d.get("on_merge") or time.time() - float(store.state(c, "train_at") or 0) < 60:
        return
    store.state(c, "train_at", time.time())
    gh_json = _d().gh_json
    runs = gh_json("run", "list", "--workflow", d["workflow"], "--limit", "10", "--json", "headSha,status,conclusion,databaseId")
    if runs and runs[0]["status"] != "completed":
        return
    head = (git("ls-remote", "origin", "refs/heads/" + base_branch()).stdout.split() or [None])[0]
    if not head:
        return
    done = next((r["headSha"] for r in runs if r["conclusion"] == "success"), None)
    if head in (done, store.state(c, "deploy_skip")):
        c.execute("DELETE FROM state WHERE k IN ('deploy_pending', 'deploy_now')")
        return
    if runs and runs[0]["headSha"] == head and runs[0]["conclusion"] != "success":
        if store.state(c, "deployfail:" + head) is None:
            store.state(c, "deployfail:" + head, 1)
            store.send(c, "system", owner(), "task", "The deploy of %s failed (run %s) and isn't retried on the same commit. See "
                       "`gh run view %s --log-failed`; the next merge (or `gh workflow run %s --ref %s`) tries again."
                       % (head[:7], runs[0]["databaseId"], runs[0]["databaseId"], d["workflow"], base_branch()))
        return
    cmp = gh_json("api", "repos/%s/compare/%s...%s" % (_d().repo_slug(), done or head + "~1", head))
    skip = re.compile(d.get("skip_paths") or r"^docs/")
    if all(skip.search(f["filename"]) for f in cmp.get("files") or []):
        store.state(c, "deploy_skip", head)   # notes only: nothing to restart for
        return
    since = float(store.state(c, "deploy_pending") or 0)
    if not since:
        since = time.time()
        store.state(c, "deploy_pending", since)
    urgent = store.state(c, "deploy_now") or any(cm["commit"]["message"].lower().startswith("revert") for cm in cmp.get("commits") or [])
    if not urgent and time.time() - since < int(d.get("every_min", 30)) * 60:
        return
    out = _d().gh("workflow", "run", d["workflow"], "--ref", base_branch())
    if out.returncode != 0:
        print("deploy train", out.stderr[:200], flush=True)
        return
    c.execute("DELETE FROM state WHERE k IN ('deploy_pending', 'deploy_now')")
    print("deploy train: %d commit(s) to %s%s" % (len(cmp.get("commits") or []), head[:7], " (urgent)" if urgent else ""), flush=True)


def deployed(c, n, cache):
    """(closed?, what happened) for a promise that depends on pr:<n>: merged, and deployed when there's a train."""
    gh_json = _d().gh_json
    pr = gh_json("pr", "view", str(n), "--json", "state,mergeCommit")
    if pr["state"] == "CLOSED":
        return True, "PR #%s was closed without merging." % n
    if pr["state"] != "MERGED":
        return False, ""
    d = config.team().deploy or {}
    if not d.get("workflow") or d.get("on_merge"):
        return True, "PR #%s was merged%s." % (n, " (the platform deploys on merge)" if d.get("on_merge") else "")
    sha = (pr.get("mergeCommit") or {}).get("oid")
    if "runs" not in cache:
        cache["runs"] = gh_json("run", "list", "--workflow", d["workflow"], "--limit", "10", "--json", "headSha,status,conclusion")
    mine = [r for r in cache["runs"] if r["status"] == "completed" and sha and (r["headSha"] == sha or contains(r["headSha"], sha))][:1]
    return (True, "PR #%s was merged and the deploy finished (%s)." % (n, mine[0]["conclusion"])) if mine else (False, "")


# ---- post-deploy verification ---------------------------------------------------------------------------------------

VERIFY_TASK = """Post-deploy check: {what}
{evidence}
{action} Find the cause, fix it in a new PR with a test that would have caught it, and say in one line in the PR why
the checks before the merge didn't."""


def http_status(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "agent-team-verify"}), timeout=20) as r:
                return r.status
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == tries - 1:
                return e.code
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                return 0
        time.sleep(5)
    return 0


def page_urls(files, page_map):
    """Changed files → site paths, by deploy.verify.page_map ([[regex, "/\\\\1"], …]); dynamic routes ("[slug]") skipped."""
    out = []
    for f in files:
        for rx, repl in page_map or []:
            m = re.fullmatch(rx, f)
            if m and "[" not in f:
                path = "/" + "/".join(p for p in m.expand(repl).split("/") if p and not p.startswith("("))
                out.append(path)
                break
    return out


def revert(n, sha, why):
    """A PR that reverts merged PR n. Returns its number or None."""
    wt, br = os.path.join(config.data_dir(), "work", "revert-%d" % n), "revert/pr-%d" % n
    git("fetch", "-q", "origin", base_branch())
    git("worktree", "remove", "--force", wt)
    if git("worktree", "add", "-q", "-B", br, wt, "origin/" + base_branch()).returncode != 0:
        return None
    try:
        if git("revert", "--no-edit", sha, cwd=wt).returncode != 0 or git("push", "-q", "-f", "origin", br, cwd=wt).returncode != 0:
            git("revert", "--abort", cwd=wt)
            return None
        out = _d().gh("pr", "create", "--head", br, "--base", base_branch(), "--title", "revert: #%d (post-deploy check)" % n,
                      "--body", "Undoes #%d, which broke production:\n\n%s\n\nOpened by the dispatcher; merges when CI passes." % (n, why))
        m = re.search(r"/pull/(\d+)", out.stdout)
        return int(m.group(1)) if m else None
    finally:
        git("worktree", "remove", "--force", wt)


def recent_deploys(d):
    """[(id, finished at, {pr number: {"sha", "title", "files"}})], newest first."""
    gh_json, slug = _d().gh_json, _d().repo_slug()
    out = []
    if d.get("workflow") and not d.get("on_merge"):
        runs = [r for r in gh_json("run", "list", "--workflow", d["workflow"], "--limit", "5", "--json", "databaseId,headSha,status,conclusion,updatedAt")
                if r["status"] == "completed" and r["conclusion"] == "success"]
        for i, run in enumerate(runs):
            prev = runs[i + 1]["headSha"] if i + 1 < len(runs) else run["headSha"] + "~1"
            prs = {}
            for cm in gh_json("api", "repos/%s/compare/%s...%s" % (slug, prev, run["headSha"])).get("commits") or []:
                first = cm["commit"]["message"].splitlines()[0]
                m = re.search(r"\(#(\d+)\)\s*$", first)
                if m:
                    prs[int(m.group(1))] = {"sha": cm["sha"], "title": first, "files": None}
            out.append(("run:%s" % run["databaseId"], ts(run["updatedAt"]), prs))
    else:
        since = (dt.datetime.now() - dt.timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M")
        for pr in gh_json("pr", "list", "--state", "merged", "--search", "merged:>=%s" % since, "--json", "number,title,mergedAt,mergeCommit,files"):
            out.append(("pr:%d" % pr["number"], ts(pr["mergedAt"]), {pr["number"]: {"sha": (pr.get("mergeCommit") or {}).get("oid"),
                        "title": pr["title"], "files": [f["path"] for f in pr.get("files") or []]}}))
    return out


def fresh_errors(c, v, since):
    """New error shapes in logs_command's output since `since` (lines start with an ISO timestamp)."""
    if not v.get("logs_command"):
        return {}
    try:
        logs = subprocess.run(v["logs_command"], cwd=config.team().base, capture_output=True, text=True, timeout=60).stdout or ""
    except Exception as e:  # noqa: BLE001
        print("verify logs", repr(e)[:200], flush=True)
        return {}
    fresh = {}
    for line in logs.splitlines():
        try:
            at = dt.datetime.fromisoformat(line.split(" ", 1)[0].replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        h = _d().signature(line.split(" ", 1)[-1])
        if at >= since and time.time() - float(store.state(c, "err:" + h) or 0) > 30 * 86400:
            fresh.setdefault(h, {"n": 0, "sample": line[:300]})["n"] += 1
    return fresh


def post_deploy(c):
    t = config.team()
    d = t.deploy or {}
    v = d.get("verify")
    if not v or time.time() - float(store.state(c, "verify_at") or 0) < 120:
        return
    store.state(c, "verify_at", time.time())
    after, site = int(v.get("after_min", 10)) * 60, v.get("site", "").rstrip("/")
    gh_json = _d().gh_json
    for key_id, at, prs in recent_deploys(d):
        key = "verified:%s" % key_id
        if store.state(c, key) or time.time() - at < after:
            continue
        if time.time() - at > 6 * 3600:   # first start, or a long pause: too late to judge
            store.state(c, key, "old")
            continue
        store.state(c, key, "checking")
        for n, pr in prs.items():
            if pr["files"] is None:
                pr["files"] = [f["filename"] for f in (gh_json("api", "repos/%s/commits/%s" % (_d().repo_slug(), pr["sha"])).get("files") or [])]
        broken = {}
        home_bad = []
        for path in v.get("paths") or ["/"]:
            code = http_status(site + path) if site else 200
            if code != 200:
                home_bad.append("%s answered %s" % (site + path, code or "nothing"))
        if home_bad:
            for n in prs:
                broken.setdefault(n, []).extend(home_bad)
        for n, pr in prs.items():
            for url in page_urls(pr["files"], v.get("page_map")):
                code = http_status(site + url)
                if code >= 500 or code == 0:
                    broken.setdefault(n, []).append("%s answered %s" % (url, code or "nothing"))
        fresh = fresh_errors(c, v, at)
        errors = ["%dx %s" % (g["n"], g["sample"]) for g in fresh.values()]
        heavy = any(g["n"] >= int(v.get("revert_errors", 5)) for g in fresh.values())
        skip = re.compile(d.get("skip_paths") or r"^docs/")
        code_prs = [n for n, pr in prs.items() if not pr["title"].lower().startswith("revert") and any(not skip.search(f) for f in pr["files"])]
        if heavy:   # a log line doesn't say which PR: every code PR of the deploy
            for n in code_prs:
                broken.setdefault(n, []).append("new error in the logs after the deploy: " + "; ".join(errors[:2]))
        result = []
        for n, pr in prs.items():
            branch = (gh_json("pr", "view", str(n), "--json", "headRefName") or {}).get("headRefName", "")
            to, th = _d().pr_author(c, branch) or owner(), thread_of_pr(c, n)
            if n in broken:
                why = "\n".join("- " + e for e in broken[n])
                if pr["title"].lower().startswith("revert"):
                    store.send(c, "system", owner(), "task", VERIFY_TASK.format(what="revert #%d is broken too" % n, evidence=why,
                               action="A revert isn't reverted: look now."), thread=th)
                    continue
                rn = revert(n, pr["sha"], why) if v.get("revert", True) else None
                action = ("The dispatcher opened revert #%d, which merges on green; your PR is out." % rn) if rn else \
                    "Nothing was reverted automatically: revert it yourself now if it's yours."
                store.send(c, "system", to, "task", VERIFY_TASK.format(what="PR #%d (%s) broke production" % (n, pr["title"]),
                           evidence=why, action=action), thread=th)
                if rn:
                    head = gh_json("pr", "view", str(rn), "--json", "headRefOid")["headRefOid"]
                    store.state(c, "verdict:%d" % rn, json.dumps({"head": head, "verdict": "ok", "thread": th, "at": time.time()}))
                    store.state(c, "review:pr:%d" % rn, head)
                if home_bad:
                    notify.say("The site (%s) is down after the deploy of PR #%d. %s" % (site, n, action[:200]),
                               who=t.display(t.orchestrator), ping=list(t.humans))
                result.append("#%d %s" % (n, "reverted" if rn else "broken"))
            elif errors and n in code_prs:
                store.send(c, "system", to, "task", VERIFY_TASK.format(
                    what="new error in the logs after the deploy of #%d (%s)" % (n, pr["title"]), evidence="\n".join("- " + e for e in errors[:4]),
                    action="Below the revert line (%s), so nothing was undone; it may not be yours." % v.get("revert_errors", 5)), thread=th)
                result.append("#%d new error" % n)
        for h in fresh:
            store.state(c, "err:" + h, time.time())
        store.state(c, key, "; ".join(result) or "ok")
        print("verified deploy", key_id, store.state(c, key), flush=True)


# ---- conflicts ------------------------------------------------------------------------------------------------------

CONFLICT_TASK = """PR #{n} ({title}) conflicts with {base} and can't go in. In a worktree: `git fetch origin && git rebase
origin/{base}` on branch {branch}, resolve keeping what's already in {base}, run the checks and `git push
--force-with-lease`. If the PR no longer makes sense (another PR did it, or {base} moved on), close it with
`gh pr close {n} --comment "<why>"`. Without a new push in {hours} h the dispatcher closes it (the branch stays)."""
REBASING = {}   # PR number → (head being rebased, None while running / True pushed / False the author's job)


def union(text):
    """A file with every conflict hunk resolved by keeping both sides: the base's lines, then the PR's (a heading both
    added stays once). The diff3 base section, if any, goes."""
    out, side, ours, theirs = [], None, [], []
    for line in text.splitlines(keepends=True):
        if line.startswith("<<<<<<< "):
            side, ours, theirs = "ours", [], []
        elif side and line.startswith("||||||| "):
            side = "base"
        elif side and line.rstrip("\r\n") == "=======":
            side = "theirs"
        elif side and line.startswith(">>>>>>> "):
            for ln in ours + [x for x in theirs if not (x.startswith("#") and x in ours)]:
                if not (ln.strip() == "" and out and out[-1].strip() == ""):
                    out.append(ln)
            side = None
        elif side == "ours":
            ours.append(line)
        elif side == "theirs":
            theirs.append(line)
        elif side is None:
            out.append(line)
    return "".join(out)


def rebase_union_only(branch, cf):
    """Rebases `branch` on the base in a throwaway worktree. When every conflict is in union_files, keeps both sides,
    runs `check` and pushes with --force-with-lease: True. Any other conflict or a red check: False, nothing pushed."""
    files = set(cf.get("union_files") or [])
    if not files:
        return False
    wt = os.path.join(config.data_dir(), "work", "rebase-%s" % re.sub(r"[^\w.-]", "_", branch))
    git("fetch", "-q", "origin", base_branch(), branch)
    git("worktree", "remove", "--force", wt)
    if git("worktree", "add", "-q", "-B", "rebase-tmp-" + re.sub(r"[^\w.-]", "_", branch), wt, "origin/" + branch).returncode != 0:
        return False
    try:
        r = git("-c", "user.name=agent-team", "-c", "user.email=agent-team@localhost", "rebase", "origin/" + base_branch(), cwd=wt, timeout=300)
        for _ in range(200):
            if r.returncode == 0:
                break
            conflicted = set(git("diff", "--name-only", "--diff-filter=U", cwd=wt).stdout.split())
            if not conflicted or not conflicted <= files:
                git("rebase", "--abort", cwd=wt)
                return False
            for f in conflicted:
                p = os.path.join(wt, f)
                with open(p, encoding="utf-8") as fh:
                    merged = union(fh.read())
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(merged)
                git("add", "--", f, cwd=wt)
            r = subprocess.run(["git", "-C", wt, "-c", "core.editor=true", "rebase", "--continue"], capture_output=True, text=True, timeout=300)
        else:
            git("rebase", "--abort", cwd=wt)
            return False
        if cf.get("check") and subprocess.run(cf["check"], cwd=wt, capture_output=True, timeout=int(cf.get("check_timeout", 900))).returncode != 0:
            return False
        return git("push", "-q", "--force-with-lease", "origin", "HEAD:refs/heads/" + branch, cwd=wt).returncode == 0
    except Exception as e:  # noqa: BLE001
        print("rebase", branch, repr(e)[:200], flush=True)
        return False
    finally:
        git("worktree", "remove", "--force", wt)


def conflicts(c):
    t = config.team()
    cf = t.conflicts
    if cf is None or time.time() - float(store.state(c, "conflicts_at") or 0) < 600:
        return
    store.state(c, "conflicts_at", time.time())
    hours = int(cf.get("close_after_h", 24))
    for pr in _d().gh_json("pr", "list", "--state", "open", "--json", "number,title,isDraft,mergeable,headRefName,headRefOid"):
        n, key = pr["number"], "conflict:%d" % pr["number"]
        seen = json.loads(store.state(c, key) or "null")
        if pr["mergeable"] != "CONFLICTING" or pr["isDraft"]:
            if seen:
                c.execute("DELETE FROM state WHERE k = ?", (key,))
            continue
        if seen and seen["head"] == pr["headRefOid"]:
            if hours and time.time() - seen["at"] >= hours * 3600:
                out = _d().gh("pr", "close", str(n), "--comment", "Closed by the dispatcher: it has conflicted with %s for %d h without a "
                              "new push. The branch is still there; reopen after the rebase." % (base_branch(), hours))
                if out.returncode == 0:
                    c.execute("DELETE FROM state WHERE k = ?", (key,))
                    store.send(c, "system", seen["to"], "update", "PR #%d (%s) closed: it conflicted for %d h without a push; branch %s kept."
                               % (n, pr["title"], hours, pr["headRefName"]), thread=seen.get("thread"))
            continue
        tried = REBASING.get(n)
        if cf.get("union_files") and (not tried or tried[0] != pr["headRefOid"]):
            REBASING[n] = (pr["headRefOid"], None)
            threading.Thread(target=lambda n=n, h=pr["headRefOid"], b=pr["headRefName"]: REBASING.__setitem__(n, (h, rebase_union_only(b, cf))),
                             daemon=True).start()
            continue
        if tried and tried[1] is not False:   # still running, or pushed (GitHub recomputes mergeable)
            continue
        to, thread = _d().pr_author(c, pr["headRefName"]) or owner(), thread_of_pr(c, n)
        store.send(c, "system", to, "task", CONFLICT_TASK.format(n=n, title=pr["title"], branch=pr["headRefName"], base=base_branch(), hours=hours),
                   thread=thread)
        store.state(c, key, json.dumps({"head": pr["headRefOid"], "at": time.time(), "to": to, "thread": thread}))
