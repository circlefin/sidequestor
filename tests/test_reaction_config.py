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
            "done": "white_check_mark", "blocked": "clock3", "expand": "magic_wand",
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

    def test_new_expand_default_preserves_existing_magic_wand_mapping(self):
        values = load_module().load_reaction_emojis({"SIDEQUESTOR_REACTION_PROCESS_EMOJI": "magic_wand"})
        self.assertEqual(values["process"], "magic_wand")
        self.assertEqual(values["expand"], "sparkles")

    def test_retry_default_preserves_existing_repeat_mapping(self):
        values = load_module().load_reaction_emojis({'SIDEQUESTOR_REACTION_LOADING_EMOJI': 'repeat'})
        self.assertEqual(values['loading'], 'repeat')
        self.assertEqual(values['blocked'], 'clock3')

    def test_clock_default_preserves_existing_clock3_mapping(self):
        values = load_module().load_reaction_emojis({'SIDEQUESTOR_REACTION_LOADING_EMOJI': 'clock3'})
        self.assertEqual(values['loading'], 'clock3')
        self.assertEqual(values['blocked'], 'clock1')

    def test_explicit_warning_override_is_preserved(self):
        values = load_module().load_reaction_emojis({'SIDEQUESTOR_REACTION_BLOCKED_EMOJI': ':warning:'})
        self.assertEqual(values['blocked'], 'warning')

    def test_bad_value_raises(self) -> None:
        module = load_module()
        with self.assertRaises(ValueError):
            module.load_reaction_emojis({"SIDEQUESTOR_REACTION_PROCESS_EMOJI": "not valid"})

    def test_new_blocked_default_preserves_existing_warning_mapping(self) -> None:
        values = load_module().load_reaction_emojis({'SIDEQUESTOR_REACTION_LOADING_EMOJI': 'warning'})
        self.assertEqual(values['loading'], 'warning')
        self.assertEqual(values['blocked'], 'clock3')


if __name__ == "__main__":
    unittest.main()
