"""Turn observed runtime failures into scoped diagnosis watches and durable reviews.

The tick owns this state and all watch changes. Checkers only read it. Nothing here
executes a repair or makes a network request.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from inbox import INBOX_QUEST, write_json

HERE = Path(__file__).resolve().parent
AUTH = re.compile(r"\bauth(?:entication|orization)?\b|\boauth\b|unauthori[sz]ed|forbidden|credential|keychain|token.expired|invalid.token|needs.login|not.logged.in|\b401\b|\b403\b", re.I)
RECURRENCE_COOLDOWN_SEC = 3600
BUG_REPORT_EMAIL = "guangmian.kung@circle.com"


def read_json(path, default):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def quest_watches(qdir):
    value = read_json(qdir / "watch.json", {}).get("watches", [])
    return [w for w in value if isinstance(w, dict)] if isinstance(value, list) else []


def safe_error(value):
    text = str(value or "")[:2000]
    text = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[redacted]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;\"']+", r"\1[redacted]", text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{10,}|(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{10,}|xox[baprs]-[A-Za-z0-9-]{10,})\b", "[redacted]", text)
    return text


def harness_problem(env):
    backend = env.get("YAAS_AGENT", "codex")
    executable = {"codex": "codex", "claude": "claude", "cursor": "cursor-agent"}.get(backend)
    # Custom runners are intentional (including fixture/stub runners).
    if env.get("YAAS_RUN_AGENT"):
        return None
    if not executable:
        return f"Unsupported harness {backend!r}; select codex, claude, or cursor in workspace configuration."
    search = f"/opt/homebrew/bin:{env.get('HOME', str(Path.home()))}/.local/bin:{env.get('PATH', os.defpath)}"
    if not shutil.which(executable, path=search):
        return (f"Selected harness {backend!r}: {executable} is not installed or is unavailable on the service PATH. "
                f"Install it, or select an installed harness. In a terminal run `command -v {executable}` "
                "and ensure the background service can resolve the same executable, then restart Sidequestor.")
    return None


class Health:
    def __init__(self, root, env, now=None):
        self.root = Path(root)
        self.env = dict(env, SIDEQUESTOR_WORKSPACE=str(root))
        self.now = time.time() if now is None else now
        self.path = self.root / "state" / "triage" / "health-issues.json"
        self.state = read_json(self.path, {"sources": {}, "issues": {}})
        self.sources = self.state.setdefault("sources", {})
        self.issues = self.state.setdefault("issues", {})

    def helper(self, command, *args):
        return subprocess.run([sys.executable, str(HERE / "ledger" / "approval-helper.py"), command,
                               *[str(a) for a in args]], env=self.env, capture_output=True, text=True, check=True)

    def reviews(self):
        return read_json(self.root / "state" / "pending-approvals.json", {}).get("items", [])

    def observe(self, source, *, reason="", quest=INBOX_QUEST, kind="checker", watch_type="", healthy=False, fresh=True,
                diagnosis_unavailable=False):
        if healthy:
            self.sources.pop(source, None)
            return
        shared = kind in {"harness", "config", "reactions", "quest_manifest"} or bool(AUTH.search(reason))
        connector = watch_type.split("_", 1)[0]
        identity = f"{kind}:{connector}:auth" if AUTH.search(reason) else f"{kind}:{source}"
        issue_id = hashlib.sha256(identity.encode()).hexdigest()[:20]
        previous = self.sources.get(source, {})
        # An error changing from auth to timeout is not recovery. Keep this
        # source in its existing incident until an actual healthy observation.
        issue_id = previous.get("issue_id", issue_id)
        owner = previous.get("owner", INBOX_QUEST if shared else quest)
        observed = self.now if fresh else self.sources.get(source, {}).get("observed_ts", self.now)
        self.sources[source] = {"issue_id": issue_id, "quest": quest, "owner": owner,
                                "observed_ts": observed,
                                "diagnosis_unavailable": diagnosis_unavailable,
                                "kind": kind, "reason": safe_error(reason), "type": watch_type}

    def save(self):
        write_json(self.path, self.state)

    def reconcile(self, can_launch=True):
        grouped = {}
        for source, record in self.sources.items():
            grouped.setdefault(record["issue_id"], []).append(dict(record, source=source))
        for iid, issue in self.issues.items():
            if iid not in grouped:
                if issue.get("active"):
                    issue["recovered_ts"] = self.now
                issue["active"] = False
        for iid, records in grouped.items():
            issue = self.issues.setdefault(iid, {"episode": 0})
            if not issue.get("active"):
                due = issue.get("recovered_ts", 0) + RECURRENCE_COOLDOWN_SEC
                if issue["episode"] and self.now < due:
                    issue.update(pending_recurrence=True, next_diagnosis_ts=due)
                else:
                    issue.update(episode=issue["episode"] + 1, attempts=0, next_diagnosis_ts=0,
                                 pending_recurrence=False)
            if issue.get("pending_recurrence") and self.now >= issue["next_diagnosis_ts"]:
                issue.update(episode=issue["episode"] + 1, attempts=0, next_diagnosis_ts=0,
                             pending_recurrence=False)
            owner = records[0]["owner"]
            if len({r["quest"] for r in records}) > 1:
                owner = INBOX_QUEST
            issue.update(active=True, owner=owner, evidence=records,
                         system_key=f"health:{iid}:{issue['episode']}")
            self.save()  # Publish the identity before approval-helper validates a producer.
            if issue.get("pending_recurrence") or self.has_review(issue):
                continue
            try:
                if not can_launch or any(r["kind"] in {"harness", "config"} or r.get("diagnosis_unavailable") for r in records):
                    self.write_review(issue, direct=True)
                    self.has_review(issue)
                else:
                    entry = {"type": "health_issue", "issue_id": iid, "reason": "Diagnose runtime failure and propose a repair for review"}
                    subprocess.run([sys.executable, str(HERE / "ledger" / "add-watch.py"), owner, json.dumps(entry)],
                                   env=self.env, capture_output=True, text=True, check=True)
                issue.pop("queue_error", None)
            except (OSError, subprocess.SubprocessError) as exc:
                issue["queue_error"] = safe_error(getattr(exc, "stderr", "") or str(exc))
                try:
                    self.write_review(issue, direct=True)
                    self.has_review(issue)
                except (OSError, subprocess.SubprocessError) as fallback:
                    issue["queue_error"] += "; " + safe_error(str(fallback))
        self.save()
        self.helper("resolve-system-items", json.dumps({
            "active_health_keys": [i["system_key"] for i in self.issues.values() if i.get("active")],
            "failed_health": {i["system_key"]: max(r.get("observed_ts", 0) for r in i["evidence"])
                              for i in self.issues.values() if i.get("active") and not i.get("pending_recurrence")}}))

    def has_review(self, issue):
        if issue.get("review_recorded") == issue["system_key"]:
            return True
        found = any(i.get("quest_id") == issue["owner"]
                    and (i.get("target", {}).get("action") == "health_repair"
                         or i.get("target", {}).get("report_kind") == "package_bug")
                    and i.get("target", {}).get("system_key") == issue["system_key"] for i in self.reviews())
        if found:
            issue["review_recorded"] = issue["system_key"]
        return found

    def write_review(self, issue, direct=False):
        evidence = issue["evidence"]
        harness = any(r["kind"] == "harness" for r in evidence)
        mode = "interactive" if harness or any(AUTH.search(r["reason"]) for r in evidence) else "uncertain"
        title = "Restore the selected harness" if harness else "Investigate Sidequestor runtime failure"
        details = "\n".join(f"- {r['quest']} / {r['source']}: {r['reason']}" for r in evidence)
        message = (f"{title}\n\nRepair mode: {mode}.\n\n{details}\n\n"
                   "Open a terminal in this workspace. Follow the recovery steps above where supplied; "
                   "for login/consent errors, authenticate the named harness or connector with the intended account. "
                   "Otherwise inspect the checker/service logs and configuration before changing anything. "
                   "Afterward run `sq doctor` and allow a successful checker or harness run to verify recovery. "
                   "An approval alone does not prove recovery.")
        self.helper("write", json.dumps({"quest_id": issue["owner"], "source": "system-health",
            "action_type": "remote_request", "target": {"system_key": issue["system_key"],
                "action": "health_repair", "repair_mode": mode}, "message_text": message,
            "context": "Automatic diagnosis is unavailable or did not produce a repair proposal; operator investigation is required." if direct else title,
            "risk_reason": title}))


def diagnosis(root, entry, now=None):
    from sidequestor import __version__
    h = Health(root, os.environ, now)
    issue = h.issues.get(entry.get("issue_id"), {})
    if (not issue.get("active") or h.has_review(issue)
            or issue.get("next_diagnosis_ts", 0) > h.now):
        return None
    return ("HEALTH DIAGNOSIS ONLY. Read OPERATING.md health protocol. "
            "Investigate these failures using read-only checks, then use sq approval write to propose "
            "a repair or a package bug report. Do not apply changes or authentication. "
            f"Verified running Sidequestor version: {__version__}; ignore stale version claims in evidence. "
            "Configuration/authentication repairs use action=health_repair and repair_mode=headless|interactive|uncertain. "
            "If the defect is in packaged code, do NOT propose editing installed/package source files, "
            "changing watch state to bypass it, or upgrading unless a released fix is verified. "
            f"Instead propose a sanitized email bug report to {BUG_REPORT_EMAIL}: target surface=gmail, action=send, "
            f"to={BUG_REPORT_EMAIL}, subject describing the defect, report_kind=package_bug, "
            f"installed_version={__version__}. Put only the email body in message_text. Include verified vs suspected "
            "findings, minimal synthetic reproduction and impact; exclude secrets, private messages, people, "
            "channel/thread IDs and internal links by default. Send only after approval through send_fresh.py. "
            f"Either approval target MUST include system_key={issue['system_key']!r}. "
            f"Approval quest_id MUST be {issue['owner']}. Evidence: {json.dumps(issue['evidence'])}")


def collect(t):
    """Observe real check results; backoff/skip is not evidence of recovery."""
    h = Health(t.repo_root, t.env, t.now_ts)
    problem = harness_problem(t.env)
    h.observe("harness", reason=problem, kind="harness", healthy=problem is None)
    live = set()
    for qdir in t.quests_dir.iterdir():
        for w in quest_watches(qdir):
            if w.get("type") != "health_issue" and t.cfg.checker_enabled(w.get("type")):
                live.add(f"{qdir.name}|{w.get('watch_id')}")
    for source in list(h.sources):
        if "|" in source and source not in live:
            h.sources.pop(source)
        elif source.startswith("dispatch:") and source != "dispatch:reactions" and not (t.quests_dir / source[9:]).is_dir():
            h.sources.pop(source)
        elif source in {"reactions-check", "dispatch:reactions"} and not getattr(t, "slack_checkers_enabled", True):
            h.sources.pop(source)
    health = read_json(t.checker_health, {})
    counts = read_json(t.unacked_file, {})
    for row in t.results:
        if row.get("type") == "health_issue":
            continue
        source = f"{row['qid']}|{row.get('watch_id')}"
        status = row["status"]
        rec = health.get(row.get("watch_id"), {})
        reason = row.get("reason", "") or rec.get("last_error", "")
        if status == "misconfig" or (status == "backoff" and
                (AUTH.search(reason) or rec.get("consecutive_errors", 0) >= getattr(t, "error_promote", 3))):
            reason = rec.get("last_error") or counts.get(source, {}).get("last_error") or reason
            h.observe(source, reason=reason, quest=row["qid"], watch_type=row.get("type", ""),
                      kind="checker" if row.get("watch_id") else "quest_manifest",
                      fresh=status == "misconfig" or row.get("checked_error", False))
        elif status in {"ok", "clean", "dirty", "hold"}:
            h.observe(source, healthy=True)
    if getattr(t, "reaction_check_error", None):
        failures = h.state.get("reaction_failures", 0) + 1
        h.state["reaction_failures"] = failures
        if failures >= 3 or AUTH.search(t.reaction_check_error) or "BAD_ARGS" in t.reaction_check_error:
            h.observe("reactions-check", kind="reactions", reason=t.reaction_check_error, watch_type="slack")
    elif getattr(t, "reaction_check_ok", False):
        h.state["reaction_failures"] = 0
        h.observe("reactions-check", healthy=True)
    h.reconcile(can_launch=problem is None)
    prune_health_watches(h, t.quests_dir)
    # Refresh health rows after recovery detection, so stale issues cannot dispatch.
    t.results[:] = [r for r in t.results if r.get("type") != "health_issue"]
    for qdir in t.quests_dir.iterdir():
        for w in quest_watches(qdir):
            if w.get("type") != "health_issue":
                continue
            text = diagnosis(t.repo_root, w, t.now_ts)
            rec = counts.get(f"{qdir.name}|{w.get('watch_id')}", {})
            if (text and h.issues.get(w.get("issue_id"), {}).get("owner") == qdir.name
                    and float(rec.get("next_retry_ts") or 0) <= t.now_ts):
                t.results.append({"qid": qdir.name, "watch_id": w["watch_id"], "type": "health_issue",
                    "status": "dirty", "preview": text, "reason": text, "advance_to": None, "complete": True})
    return problem


def prune_health_watches(h, quests_dir):
    import fcntl
    for qdir in quests_dir.iterdir():
        path = qdir / "watch.json"
        if not qdir.is_dir() or not path.exists():
            continue
        with open(path.with_name("watch.json.lock"), "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = read_json(path, {})
            watches = data.get("watches")
            if not isinstance(watches, list):
                continue
            kept = [w for w in watches if not (isinstance(w, dict) and w.get("type") == "health_issue"
                    and (not h.issues.get(w.get("issue_id"), {}).get("active")
                         or h.issues.get(w.get("issue_id"), {}).get("owner") != qdir.name))]
            if len(kept) != len(watches):
                write_json(path, dict(data, watches=kept))


def preflight(t):
    h = Health(t.repo_root, t.env, t.now_ts)
    problem = harness_problem(t.env)
    h.observe("configuration", healthy=True)
    h.observe("harness", reason=problem, kind="harness", healthy=problem is None)
    h.reconcile(can_launch=problem is None)
    return problem


def configuration_failure(reason):
    """A bad setting can prevent Tick construction; the review path needs no Config."""
    import fcntl
    import tick_state
    root = tick_state._repo_root(HERE)
    env = tick_state.load_environment(root)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "triage.lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        h = Health(root, env)
        h.helper("ensure-inbox")
        h.observe("configuration", kind="config", reason=str(reason))
        h.reconcile(can_launch=False)


def dispatch_result(t, target):
    if t.dispatch_exit == 8:  # No invocation occurred; this is neither failure nor recovery.
        return
    h = Health(t.repo_root, t.env)
    source = f"dispatch:{target}"
    prev = h.state.setdefault("dispatch_failures", {}).get(target, 0)
    failed = t.dispatch_exit not in (0, 8)
    h.state["dispatch_failures"][target] = prev + 1 if failed else 0
    if failed:
        reason = t.dispatch_last_error or f"Harness exited {t.dispatch_exit}; inspect the latest worker log."
        if AUTH.search(reason) or prev + 1 >= 3 or getattr(t, "dispatch_launch_failed", False) or t.dispatch_exit in (126, 127):
            h.observe(source, kind="harness" if getattr(t, "dispatch_launch_failed", False) or t.dispatch_exit in (126, 127) else "dispatch",
                      reason=reason, quest=INBOX_QUEST if target == "reactions" else target,
                      diagnosis_unavailable=True)
    else:
        h.observe(source, healthy=True)
    # A diagnosis that exits without a proposal must not dispatch every minute,
    # even if its ack said handled. Existing no-progress backoff also still applies.
    dispatched_ids = {w.get("watch_id") for w in getattr(t, "dirty_watches", [])
                      if w.get("quest_id") == target and w.get("type") == "health_issue"}
    issue_ids = {w.get("issue_id") for w in quest_watches(t.quests_dir / target)
                 if w.get("watch_id") in dispatched_ids}
    for iid in issue_ids:
        issue = h.issues.get(iid, {})
        if issue.get("owner") == target and issue.get("active") and not h.has_review(issue):
            attempts = issue.get("attempts", 0) + 1
            issue.update(attempts=attempts, next_diagnosis_ts=h.now + min(86400, 300 * 2 ** min(attempts - 1, 9)))
            if attempts >= 3:
                try:
                    h.write_review(issue, direct=True)
                    h.has_review(issue)
                except (OSError, subprocess.SubprocessError) as exc:
                    issue["queue_error"] = safe_error(str(exc))
    h.reconcile(can_launch=harness_problem(t.env) is None)


def sync_updates(t):
    try:
        from sidequestor import __version__
        from sidequestor.update_check import available_update, check_enabled, read_status
    except ImportError:
        return
    h = Health(t.repo_root, t.env)
    from sidequestor.upgrade_job import schedule_reviewed
    status = read_status(t.repo_root)
    h.helper("resolve-system-items", json.dumps({"installed": __version__, "latest": status.get("latest")}))
    schedule_reviewed(t.repo_root, t.env)
    if not check_enabled(t.env):
        return
    update = available_update(status, __version__)
    notices = h.state.setdefault("update_notices", {})
    if update and update["latest"] not in notices:
        h.helper("write", json.dumps({"quest_id": INBOX_QUEST, "source": "update-check",
            "action_type": "remote_request", "target": {"system_key": f"upgrade:{update['latest']}",
                "action": "sidequestor_upgrade", "version": update["latest"], "repair_mode": "headless", "execution_protocol": "independent-v1"},
            "message_text": f"Sidequestor {update['latest']} is available; installed: {update['installed']}.\n\n"
                "Approve to run an independent upgrade job after the current tick finishes. It installs the latest stable "
                "release (at least the version shown), refreshes managed resources, validates with sq doctor, "
                "and restarts previously running workspace jobs. The job records its outcome in Inbox. "
                "No interactive session is needed unless installation, permissions or authentication fail. "
                "You may instead run sq upgrade in a separate terminal.\n\n"
                f"Release: {update['url']}",
            "context": "A newer stable release was detected by the PyPI update check.",
            "risk_reason": f"Upgrade Sidequestor to {update['latest']}"}))
        notices[update["latest"]] = True
        h.save()
