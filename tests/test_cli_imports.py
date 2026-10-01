from __future__ import annotations

import os
import json
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
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
    @unittest.skipIf(sys.platform == "win32", "POSIX SIGINT subprocess test")
    def test_cli_run_without_tk_closes_cleanly_on_sigint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "settings.json").write_text(
                json.dumps({"proxy": {"__type": "URL", "data": "http://127.0.0.1:9"}}),
                encoding="utf8",
            )
            code = """
import runpy
import sys

for module in ("tkinter", "tkinter.messagebox", "pystray", "PIL", "PIL.Image", "PIL.ImageTk"):
    sys.modules[module] = None
sys.argv = ["main.py", "-vv", "cli", "run"]
runpy.run_path("main.py", run_name="__main__")
"""
            environment = os.environ.copy()
            environment["TDM_DATA_DIR"] = str(data_dir)
            process = subprocess.Popen(
                [str(PYTHON), "-u", "-c", code],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            lines: queue.Queue[str] = queue.Queue()
            assert process.stdout is not None
            reader = threading.Thread(
                target=lambda: [lines.put(line) for line in process.stdout], daemon=True
            )
            reader.start()
            output: list[str] = []
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        line = lines.get(timeout=min(0.5, max(0.01, deadline - time.monotonic())))
                    except queue.Empty:
                        if process.poll() is not None:
                            break
                        continue
                    output.append(line)
                    if "Login flow started" in line or "Cannot connect to Twitch" in line:
                        break
                else:
                    self.fail("CLI did not reach login: " + "".join(output[-20:]))
                self.assertIsNone(process.poll(), "".join(output[-20:]))
                process.send_signal(signal.SIGINT)
                self.assertEqual(process.wait(timeout=15), 0, "".join(output[-20:]))
                reader.join(timeout=1)
                while not lines.empty():
                    output.append(lines.get_nowait())
                self.assertNotIn("ImportError", "".join(output))
                self.assertNotIn("Traceback", "".join(output))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()

    def test_cli_without_subcommand_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_python("main.py", "cli", data_dir=Path(directory))
        self.assertEqual(result.returncode, 2, result.stderr)
