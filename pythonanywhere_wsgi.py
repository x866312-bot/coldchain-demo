"""WSGI entrypoint for a PythonAnywhere Manual Configuration web app."""
import os
import sys

PROJECT_DIR = os.environ.get(
    "COLDCHAIN_PROJECT_DIR",
    os.path.expanduser("~/coldchain-demo"),
)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

os.environ.setdefault(
    "COLDCHAIN_OUTPUT_DIR",
    os.path.expanduser("~/coldchain-data"),
)
os.environ.setdefault("COLDCHAIN_HTTPS", "1")

from cloud_app import app as application
