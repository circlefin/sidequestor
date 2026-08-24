from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REACTION_CONFIG = ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage" / "reaction_config.py"


def load_module():
    spec = importlib.util.spec_from_file_location("reaction_config_test", REACTION_CONFIG)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ReactionConfigTest(unittest.TestCase):
    def test_defaults_are_standard_unique_names(self) -> None:
        module = load_module()
        values = module.load_reaction_emojis({})
        self.assertEqual(values, {
            "process": "robot_face", "draft": "writing_hand", "save": "floppy_disk",
            "adopt": "incoming_envelope", "loading": "hourglass_flowing_sand",
            "done": "white_check_mark",
        })
        self.assertEqual(len(values), len(set(values.values())))
        for value in values.values():
            self.assertRegex(value, module.EMOJI_NAME)

    def test_canonical_override_wins_and_colons_are_stripped(self) -> None:
        module = load_module()
        values = module.load_reaction_emojis({
            "YAAS_REACTION_PROCESS_EMOJI": "legacy",
            "SIDEQUESTOR_REACTION_PROCESS_EMOJI": ":eyes:",
        })
        self.assertEqual(values["process"], "eyes")

    def test_bad_value_raises(self) -> None:
        module = load_module()
        with self.assertRaises(ValueError):
            module.load_reaction_emojis({"SIDEQUESTOR_REACTION_PROCESS_EMOJI": "not valid"})


if __name__ == "__main__":
    unittest.main()
