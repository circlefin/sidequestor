from __future__ import annotations

import unittest
from unittest.mock import patch

from sidequestor import build_info as build_info_module


class BuildInfoTest(unittest.TestCase):
    def test_missing_metadata_and_git_degrade_to_empty_commit(self) -> None:
        build_info_module._BUILD_INFO = None
        with patch.object(build_info_module.metadata, "distribution", side_effect=Exception("absent")), \
                patch.object(build_info_module.subprocess, "run", side_effect=Exception("no git")):
            info = build_info_module.build_info()
        self.assertEqual(info["commit"], "")
        self.assertEqual(info["commit_full"], "")
        self.assertEqual(info["source"], "unknown")
        build_info_module._BUILD_INFO = None


if __name__ == "__main__":
    unittest.main()
