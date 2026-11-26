"""Windows-specific GUI configuration regression tests."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

HOST_DIR = Path(__file__).resolve().parent.parent


@unittest.skipUnless(sys.platform == "win32", "Windows Qt configuration only")
class WindowsGuiConfigurationTests(unittest.TestCase):
    def test_windows_configuration_and_key_mapping(self) -> None:
        env = os.environ.copy()
        env.pop("QT_QPA_PLATFORMTHEME", None)
        env.pop("QT_STYLE_OVERRIDE", None)
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import os, deckgui; from pathlib import Path; "
                "assert 'QT_STYLE_OVERRIDE' not in os.environ; "
                "assert 'QT_QPA_PLATFORMTHEME' not in os.environ; "
                "assert deckgui.CONFIG_DIR == Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'StreamDeck'; "
                "deckgui.key_token_validator('super'); "
                "deckgui.key_token_validator('f12')",
            ],
            check=True,
            cwd=HOST_DIR,
            env=env,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
