from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from sidequestor import __version__


class VersionTest(unittest.TestCase):
    def test_runtime_version_matches_project_metadata(self) -> None:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        metadata = tomllib.loads(pyproject.read_text())
        self.assertEqual(__version__, metadata["project"]["version"])


if __name__ == "__main__":
    unittest.main()
