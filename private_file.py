"""Owner-only access for files that hold secrets: Windows ignores POSIX mode bits."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"


def _process_sid() -> str:
    # whoami ships with Windows and reports the identity of this process, not of %USERNAME%
    result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                            capture_output=True, text=True, check=False)
    sid = result.stdout.strip().rsplit(",", 1)[-1].strip().strip('"')
    if result.returncode != 0 or not sid.startswith("S-1-"):
        raise OSError("cannot determine the account SID of this process")
    return sid


def restrict_to_owner(path: Path) -> None:
    """Leave only a full-access entry for this process' account; raise OSError otherwise."""
    if not WINDOWS:
        return
    sid = _process_sid()
    # /reset drops explicit entries an existing file may carry; /inheritance:r drops inherited ones
    for arguments in (["/reset"], ["/inheritance:r", "/grant:r", f"*{sid}:F"]):
        result = subprocess.run(["icacls", str(path), *arguments], capture_output=True, check=False)
        if result.returncode != 0:
            raise OSError(f"cannot restrict access to {path.name} (icacls exit {result.returncode})")
