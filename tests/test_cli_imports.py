from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"


def run_python(*args: str, data_dir: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["TDM_DATA_DIR"] = str(data_dir)
    return subprocess.run(
        [str(PYTHON), *args],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


class CLIImportTests(unittest.TestCase):
    def test_cli_help_and_imports_do_not_need_tk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            code = """
import runpy
import sys

for module in ("tkinter", "tkinter.messagebox", "pystray", "PIL", "PIL.Image", "PIL.ImageTk"):
    sys.modules[module] = None
sys.argv = ["main.py", "cli", "--help"]
try:
    runpy.run_path("main.py", run_name="__main__")
except SystemExit as exc:
    assert exc.code == 0, exc.code
import cli, cli_commands, ui_base, progress_timer, twitch
"""
            result = run_python("-c", code, data_dir=data_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_without_subcommand_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_python("main.py", "cli", data_dir=Path(directory))
        self.assertEqual(result.returncode, 2, result.stderr)
