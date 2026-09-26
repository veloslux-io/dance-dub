"""`python -m dance_dub` → launch the Dance Dub app."""

import sys
from pathlib import Path

# The packaged exe is windowed, so it has no console: sys.stdout/stderr are
# None and any library that writes progress to them (tqdm during the model
# download, yt-dlp) would crash. Send that output to a log file instead.
if sys.stdout is None or sys.stderr is None:
    _log_dir = Path.home() / "DanceDub"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log = open(_log_dir / "dance-dub.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

from dance_dub.app import main  # noqa: E402

if __name__ == "__main__":
    main()
