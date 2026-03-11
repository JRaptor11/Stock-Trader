# config.py
import os
import tempfile

TMP_DIR = tempfile.gettempdir()

PROGRAM_STARTUP_FILE = os.path.join(TMP_DIR, "program_last_startup_time.txt")
PROGRAM_SHUTDOWN_FILE = os.path.join(TMP_DIR, "program_last_shutdown_time.txt")
PROGRAM_SHUTDOWN_REASON_FILE = os.path.join(TMP_DIR, "program_last_shutdown_reason.txt")

ENABLE_DEV_ROUTES = False