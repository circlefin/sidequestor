"""Approved upgrades owned by a separate launchd job, never a triage child.

No LLM is needed to execute a fixed upgrade command. The runner waits for triage's
lock, claims the reviewed notice, and delegates the existing stop/install/validate/
restart lifecycle to a fresh CLI process. Failure returns the item to human review.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from .update_check import parse_version


def _valid_notice(item):
    target = item.get("target", {})
    return (item.get("quest_id") == "quest-inbox" and item.get("action_type") == "remote_request"
            and target.get("action") == "sidequestor_upgrade"
            and target.get("execution_protocol") == "independent-v1"
            and parse_version(target.get("version")) is not None
            and target.get("system_key") == f"upgrade:{target.get('version')}")


def _runtime():
    path = Path(__file__).parent / "runtime/yaas-triage"
    sys.path.insert(0, str(path))
    import approval_store
    import approval_state
    from inbox import try_log_inbox
    return approval_store, approval_state, try_log_inbox


def schedule_reviewed(root, env):
    """Called under triage's lock; never stop jobs here or dispatch an upgrade worker."""
    path = Path(root) / "state/pending-approvals.json"
    if not path.exists():
        return
    items = json.loads(path.read_text()).get("items", [])
    for item in items:
        if not _valid_notice(item) or item.get("status") != "reviewed":
            continue
        identity = f"{Path(root).resolve()}:{item['id']}:{item.get('reviewed_at')}"
        label = "com.sidequestor.upgrade." + hashlib.sha256(identity.encode()).hexdigest()[:24]
        log = Path(root) / "logs" / f"upgrade-{label.rsplit('.', 1)[1]}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform != "darwin":
                raise OSError("Independent upgrades currently require macOS launchd")
            exists = subprocess.run(["launchctl", "list", label], capture_output=True, timeout=10)
            if exists.returncode == 0:
                continue
            subprocess.run(["launchctl", "submit", "-l", label, "-o", str(log), "-e", str(log),
                            "--", sys.executable, "-m", "sidequestor.upgrade_job",
                            "--workspace", str(Path(root).resolve()), "--approval-id", item["id"],
                            "--service-path", env.get("PATH", os.defpath), "--label", label,
                            "--config-home", env.get("SIDEQUESTOR_CONFIG_HOME") or env.get("YAAS_CONFIG_HOME") or str(Path.home() / ".config")],
                           capture_output=True, text=True, timeout=15, check=True, env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            # Do not retry unattended every minute or fall back to an unsafe child.
            helper = Path(__file__).parent / "runtime/yaas-triage/ledger/approval-helper.py"
            try:
                subprocess.run([sys.executable, str(helper), "fail", item["id"],
                                f"Could not schedule independent upgrade ({type(exc).__name__}). "
                                "Run sq upgrade in a separate terminal, or approve again after repairing launchd."],
                               env=dict(env, SIDEQUESTOR_WORKSPACE=str(root)), capture_output=True, check=True)
            except (OSError, subprocess.SubprocessError):
                print(f"Upgrade scheduling and review update failed for {item['id']}; check workspace write permissions", file=sys.stderr)


def _run_lifecycle(command):
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        return subprocess.CompletedProcess(command, process.wait(timeout=1800))
    except BaseException:
        # pip/doctor/start are descendants too. Never leave an installer running
        # behind a review item that says it is safe to try again.
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        raise


def _notify_failure():
    # Restarting an invalid or half-installed package is unsafe. Surface failure
    # independently of the dashboard, which may correctly remain stopped.
    try:
        subprocess.run(["/usr/bin/osascript", "-e", 'display notification "Upgrade needs attention. Open the workspace upgrade log, run sq doctor, then sq start after repair." with title "Sidequestor upgrade failed"'],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def run(root, approval_id):
    root = Path(root)
    store, rules, log = _runtime()
    # Hold this across restart too: a freshly started tick must not race the old
    # tick's state or redispatch work before the upgrade result is durable.
    (root / "logs").mkdir(parents=True, exist_ok=True)
    with open(root / "logs/triage.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        (root / "logs/triage.lock.holder").write_text(str(os.getpid()))
        def claim(item):
            if not _valid_notice(item):
                return store.NO_WRITE
            updates = rules.apply_transition(item, "start", {}, datetime.now(timezone.utc))
            if isinstance(updates, dict):
                # Never expose Reclaim while the bounded lifecycle can still run.
                updates['lease_expires_at'] = max(updates['lease_expires_at'],
                    (datetime.now(timezone.utc) + timedelta(minutes=35)).isoformat())
            return store.NO_WRITE if updates is rules.ILLEGAL else updates
        item = store.mutate_item(approval_id, claim)
        if not isinstance(item, dict):
            return 0  # cancelled, already claimed, or removed while waiting
        code, note = 1, "Upgrade did not complete. Run sq doctor and inspect the upgrade log before retrying."
        try:
            command = [sys.executable, "-m", "sidequestor", "--workspace", str(root), "upgrade"]
            result = _run_lifecycle(command)
            if result.returncode == 0:
                version = subprocess.run([sys.executable, "-c",
                                          "from importlib.metadata import version; print(version('sidequestor'))"],
                                         capture_output=True, text=True, timeout=15, check=True).stdout.strip()
                have, want = parse_version(version), parse_version(item["target"]["version"])
                if have is not None and have >= want:
                    code, note = 0, f"Upgrade verified: installed Sidequestor {version}; resources synced, doctor passed, previously running jobs restarted."
                else:
                    note = f"Installer did not reach the reviewed version {item['target']['version']}. Check the package index and run sq doctor."
        except (OSError, subprocess.SubprocessError) as exc:
            note = f"Upgrade interrupted ({type(exc).__name__}); outcome may be partial. Run sq doctor, inspect the upgrade log, and repair permissions/authentication in a terminal if needed."
        action = "done" if code == 0 else "fail"
        store.mutate_item(approval_id, lambda current: rules.apply_transition(
            current, action, {"worker_reply": note} if code == 0 else {"reason": note}, datetime.now(timezone.utc)))
        log(root, f"upgrade:{approval_id}:{item.get('reviewed_at')}", {
            "event": "sidequestor_upgraded" if code == 0 else "sidequestor_upgrade_failed",
            "approval_id": approval_id, "note": note})
        if code:
            _notify_failure()
        return code


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--approval-id", required=True)
    parser.add_argument("--service-path", required=True)
    parser.add_argument("--config-home", required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"com\.sidequestor\.upgrade\.[0-9a-f]{24}", args.label):
        parser.error("invalid upgrade job label")
    def terminate(_signal, _frame):
        raise InterruptedError('upgrade job was terminated')
    previous_term = signal.signal(signal.SIGTERM, terminate)
    try:
        from .workspace import load_workspace
        workspace = load_workspace(args.workspace)
        # launchctl does not inherit its caller's environment. Carry only safe
        # service routing in argv; reload proxy/CA/credentials from the workspace.
        from .native import _environment
        os.environ.update(PATH=args.service_path, SIDEQUESTOR_CONFIG_HOME=args.config_home,
                          YAAS_CONFIG_HOME=args.config_home)
        os.environ.update(_environment(workspace))
        os.environ.pop("SIDEQUESTOR_DISPATCH_TARGET", None)
        return run(workspace.root, args.approval_id)
    except Exception as exc:
        # Startup can fail before a claim exists. Return even that failure to
        # review instead of silently resubmitting a broken runner every tick.
        helper = Path(__file__).parent / 'runtime/yaas-triage/ledger/approval-helper.py'
        try:
            subprocess.run([sys.executable, str(helper), 'fail', args.approval_id,
                            f'Independent upgrade job failed ({type(exc).__name__}). Inspect the upgrade log and run sq doctor before retrying.'],
                           env=dict(os.environ, SIDEQUESTOR_WORKSPACE=str(Path(args.workspace).resolve())),
                           capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
        _notify_failure()
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        # submit keeps jobs alive on failure. Retire this exact one-shot even
        # when startup/claim fails; do this only after outcome writes are durable.
        subprocess.run(["/bin/launchctl", "remove", args.label], capture_output=True, timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
