"""Resolve where the data lives, whether running from source or frozen.

A one-file PyInstaller build unpacks itself into a throwaway temporary
directory, so os.path.dirname(__file__) points at that copy rather than at the
folder the user keeps the database in. The executable's own directory is the
stable anchor, and it is what every module here resolves against.
"""

import os
import sys


def base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE = base_dir()
DB = os.path.join(BASE, "traffic.db")
DASHBOARD = os.path.join(BASE, "dashboard.html")
STOP_FILE = os.path.join(BASE, "collector.stop")


def resource(name):
    """Locate a file shipped alongside the code.

    A one-file build unpacks its bundled data under _MEIPASS, which is not the
    same place as the executable; data the user owns lives next to the exe,
    data we ship travels with the bundle.
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)
