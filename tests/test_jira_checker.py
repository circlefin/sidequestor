from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JIRA_CHECKER = ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage" / "checkers" / "jira.py"

# A stand-in for jira-call.sh that behaves like Jira on the one point under test: JQL date
# literals are parsed in the requesting user's profile time zone, not UTC.
FAKE_JIRA = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, re, sys, urllib.parse
    from datetime import datetime
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(os.environ["FAKE_JIRA_TZ"])
    issues = json.loads(os.environ["FAKE_JIRA_ISSUES"])
    path = sys.argv[2]
    if path.startswith("/rest/api/3/myself"):
        print(json.dumps({"accountId": "me", "timeZone": os.environ["FAKE_JIRA_TZ"]}))
        sys.exit(0)
    jql = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["jql"][0]
    match = re.search(r'updated >= "([0-9/]+ [0-9:]+)"', jql)
    if match:
        bound = datetime.strptime(match.group(1), "%Y/%m/%d %H:%M").replace(tzinfo=zone)
        issues = [i for i in issues
                  if datetime.fromisoformat(i["fields"]["updated"]) >= bound]
    print(json.dumps({"issues": issues, "isLast": True}))
''')


class JiraCheckerTimeZoneTests(unittest.TestCase):
    def run_checker(self, user_zone: str, since: float, updated_iso: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="sidequestor-jira-") as directory:
            fake = Path(directory) / "jira-call.sh"
            fake.write_text(FAKE_JIRA)
            fake.chmod(0o755)
            issue = {"key": "OPS-1", "fields": {
                "updated": updated_iso, "summary": "Rotate keys", "status": {"name": "Open"}}}
            environment = dict(os.environ, JIRA_CALL=str(fake), FAKE_JIRA_TZ=user_zone,
                               FAKE_JIRA_ISSUES=json.dumps([issue]))
            completed = subprocess.run(
                [sys.executable, str(JIRA_CHECKER),
                 json.dumps({"type": "jira", "jql": "project = OPS",
                             "last_checked_ts": str(since)})],
                capture_output=True, text=True, timeout=30, env=environment, check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_update_after_watermark_is_seen_when_jira_user_is_west_of_utc(self) -> None:
        # 2026-10-16 15:00 UTC watermark; the issue changed 30 minutes later. A bound
        # written in UTC is read by Jira as 15:00 New York time (19:00 UTC), which would
        # hide the change, report the watch clean, and let triage skip it for good.
        since = 1792162800.0
        result = self.run_checker("America/New_York", since, "2026-10-16T15:30:00.000+0000")
        self.assertEqual(result["outcome"], "dirty", result)
        self.assertEqual(result["count"], 1, result)

    def test_update_after_watermark_is_seen_when_jira_user_is_east_of_utc(self) -> None:
        since = 1792162800.0
        result = self.run_checker("Asia/Singapore", since, "2026-10-16T15:30:00.000+0000")
        self.assertEqual(result["count"], 1, result)

    def test_update_before_watermark_is_not_reported(self) -> None:
        since = 1792162800.0
        result = self.run_checker("America/New_York", since, "2026-10-16T14:30:00.000+0000")
        self.assertEqual(result["count"], 0, result)


if __name__ == "__main__":
    unittest.main()
