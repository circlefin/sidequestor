from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DashboardReviewPreviewTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for dashboard behavior checks")
    def test_review_preview_behavior(self) -> None:
        result = subprocess.run(
            ["node", str(ROOT / "tests" / "dashboard_review_preview.cjs")],
            cwd=ROOT, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
