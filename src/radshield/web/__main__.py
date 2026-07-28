"""Launch the calculator and open it in a browser.

    python -m radshield.web
"""

from __future__ import annotations

import argparse
import threading
import webbrowser


def main() -> None:
    """Parse arguments and run the server."""
    parser = argparse.ArgumentParser(description="Radiation shielding calculator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:  # pragma: no cover - environment dependent
        raise SystemExit("uvicorn is required. Install with: pip install 'radshield[web]'")

    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Radiation shielding calculator running at {url}")
    uvicorn.run("radshield.web.app:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
