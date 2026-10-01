"""Owner-only access for files that hold secrets: Windows ignores POSIX mode bits."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
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
    """Leave only a full-access entry for this process' account; raise OSError otherwise.

    Meant for a file just created by this process, before any secret is written: such a file has
    inherited entries only, so dropping them leaves the single grant below.
    """
    if not WINDOWS:
        return
    sid = _process_sid()
    result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:F"],
                            capture_output=True, check=False)
    if result.returncode != 0:
        raise OSError(f"cannot restrict access to {path.name} (icacls exit {result.returncode})")


def rewrite_private(path: Path, content: str) -> None:
    """Move content into a fresh owner-only file, so no looser ACL survives and none is widened."""
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        try:
            restrict_to_owner(temporary)
        except OSError:
            os.close(descriptor)
            raise
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
