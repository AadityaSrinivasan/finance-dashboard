"""Start the dashboard:  .venv\\Scripts\\python -m app"""
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .config import PORT

HOST = "127.0.0.1"  # loopback only: never reachable from other computers


def main():
    url = f"http://{HOST}:{PORT}"
    print(f"\n  Finance dashboard running at {url}\n  (Close this window or press Ctrl+C to stop.)\n")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    # reload=True: picks up code updates automatically (watches only the app folder), so you
    # don't have to restart after changes. Still bound to 127.0.0.1 only.
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="warning", access_log=False,
                reload=True, reload_dirs=[str(Path(__file__).resolve().parent)])


if __name__ == "__main__":
    main()
