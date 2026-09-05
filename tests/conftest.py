"""The tests replay the first day's recording, kept under tests/fixtures/replay, so that
re-recording data/replay from a later live run, which the app ships, never moves the numbers the
tests assert. A test that wants another directory sets SWE_LOOP_REPLAY_DIR itself."""

import os
from pathlib import Path

os.environ.setdefault(
    "SWE_LOOP_REPLAY_DIR", str(Path(__file__).resolve().parent / "fixtures" / "replay")
)
