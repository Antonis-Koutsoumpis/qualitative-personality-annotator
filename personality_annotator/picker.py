"""Native "choose folder / choose files" dialogs.

The browser cannot hand a local folder path to the tool, but because the tool
runs on the same computer, it can open the operating system's own dialog.
The dialog runs in a separate process (tkinter needs its own main thread):

    python -m personality_annotator.picker folder|files

It prints a JSON list of chosen paths (empty if cancelled).
"""
from __future__ import annotations

import json
import subprocess
import sys

from .formats import ALL_EXTENSIONS


def run_dialog(kind: str) -> list[str]:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    if kind == "folder":
        path = filedialog.askdirectory(parent=root, title="Choose a folder with items to annotate")
        paths = [path] if path else []
    else:
        patterns = " ".join(f"*{e}" for e in sorted(ALL_EXTENSIONS))
        chosen = filedialog.askopenfilenames(
            parent=root, title="Choose files to annotate",
            filetypes=[("Supported files", patterns), ("All files", "*.*")])
        paths = list(chosen)
    root.destroy()
    return paths


def pick(kind: str, timeout: float = 600) -> list[str]:
    """Open the dialog in a child process and return the chosen paths."""
    if kind not in ("folder", "files"):
        raise ValueError("kind must be 'folder' or 'files'")
    if getattr(sys, "frozen", False):          # packaged program: re-run ourselves
        cmd = [sys.executable, "--pick", kind]
    else:
        cmd = [sys.executable, "-m", "personality_annotator.picker", kind]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip().splitlines()[-1] if res.stderr.strip()
                           else "The file dialog could not be opened.")
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("[")]
    return json.loads(lines[-1]) if lines else []


def main(kind: str) -> None:
    print(json.dumps(run_dialog(kind)), flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "folder")
