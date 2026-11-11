"""`gma-demo`: run the red-team eval, then the injection demo (no network, no API key)."""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent / "_repo"


def main() -> None:
    if not _REPO.is_dir():  # running from a source checkout, not from the built wheel
        sys.exit("gma-demo must be installed as a package: uvx --from <repo> gma-demo")
    print("== Red-team eval: 15 adversarial cases against the send gate ==", flush=True)
    code = subprocess.call(
        [
            sys.executable, "-m", "pytest", "-q", "-m", "redteam",
            "-p", "no:cacheprovider", "-o", "markers=redteam: adversarial guardrail eval",
            "--rootdir", str(_REPO), str(_REPO / "mcp_server" / "tests"),
        ]
    )
    if code != 0:
        sys.exit(code)
    print("\n== Demo: injected voice note, fooled agent calls send_message ==", flush=True)
    runpy.run_path(str(_REPO / "scripts" / "demo.py"), run_name="__main__")
