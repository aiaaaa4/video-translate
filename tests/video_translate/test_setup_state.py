import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "video-translate" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from setup_state import initialized, mark_initialized  # noqa: E402
import preflight  # noqa: E402


class SetupStateTest(unittest.TestCase):
    def test_initialization_state_is_environment_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "setup.json"
            self.assertFalse(initialized(state))
            written = mark_initialized(state)
            self.assertEqual(written, state)
            self.assertTrue(initialized(state))
            if os.name != "nt":
                self.assertEqual(state.stat().st_mode & 0o077, 0)

    def test_preflight_shows_first_use_intro_only_before_initialization(self):
        first = io.StringIO()
        with mock.patch.object(preflight, "initialized", return_value=False), contextlib.redirect_stdout(first):
            preflight.main()
        self.assertIn("首次使用说明", first.getvalue())

        later = io.StringIO()
        with mock.patch.object(preflight, "initialized", return_value=True), contextlib.redirect_stdout(later):
            preflight.main()
        self.assertNotIn("首次使用说明", later.getvalue())
        self.assertIn("请一次性确认以下内容", later.getvalue())


if __name__ == "__main__":
    unittest.main()
