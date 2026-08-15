"""Single-command launcher: adds backend/ to sys.path and starts the app."""

import os
import sys
import webbrowser
from threading import Timer

import uvicorn

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))

HOST, PORT = "127.0.0.1", 8000

if __name__ == "__main__":
    Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
