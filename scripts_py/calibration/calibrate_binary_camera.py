"""Command-line entry point for offline binary camera calibration."""

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from taxi_receiver.binary_calibration import main


if __name__ == "__main__":
    raise SystemExit(main())
