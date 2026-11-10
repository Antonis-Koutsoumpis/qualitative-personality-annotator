"""Start the annotation tool locally and open it in the browser.

    python -m personality_annotator --config config.yaml
"""
from __future__ import annotations

import argparse
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__

BUNDLED_EXAMPLE = Path(__file__).parent / "config.example.yaml"


def _app_dir() -> Path:
    # Next to the executable when packaged, otherwise the current folder.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _free_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 50)):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("No free port found.")


def _pause_if_frozen() -> None:
    """Keep the console window open when the packaged app was double-clicked."""
    if getattr(sys, "frozen", False):
        try:
            input("Press Enter to close...")
        except EOFError:
            pass


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--pick"]:  # native file dialog, used by the packaged program
        from .picker import main as pick_main
        pick_main(argv[1] if len(argv) > 1 else "folder")
        return
    ap = argparse.ArgumentParser(prog="personality-annotator", description=__doc__)
    ap.add_argument("--config", "-c", help="Path to config.yaml (default: ./config.yaml)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 = only this computer (default); 0.0.0.0 = local network")
    ap.add_argument("--port", "-p", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="Don't open a browser tab")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)  # show messages immediately
    except Exception:
        pass

    cfg = Path(args.config) if args.config else _app_dir() / "config.yaml"
    if not cfg.exists():
        if args.config:
            raise SystemExit(f"Config file not found: {cfg}")
        shutil.copy(BUNDLED_EXAMPLE, cfg)
        print(f"No config.yaml found, so an example was created at:\n  {cfg}\n"
              "Edit it (sources, variables), then start the tool again.")
        _pause_if_frozen()
        return

    import uvicorn
    from .app import create_app
    from .config import ConfigError

    try:
        app = create_app(cfg)
    except ConfigError as exc:
        print(f"Problem in {cfg}:\n  {exc}")
        _pause_if_frozen()
        raise SystemExit(1)

    port = _free_port(args.port) if args.host == "127.0.0.1" else args.port
    url = f"http://127.0.0.1:{port}/"
    print(f"Personality Annotator {__version__}")
    print(f"  config : {cfg}")
    print(f"  data   : {app.state.config.data_dir}")
    print(f"  open   : {url}   (admin: {url}admin)")
    print("Close this window (or press Ctrl+C) to stop.")
    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(url)),
                         daemon=True).start()
    uvicorn.run(app, host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
