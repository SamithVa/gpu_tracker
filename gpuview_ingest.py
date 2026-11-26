"""Run the SSH receiver from the path used by existing keys."""

from pathlib import Path
from runpy import run_path


if __name__ == "__main__":
    # The forced command uses -I, so resolve the receiver without a local import.
    receiver = Path(__file__).resolve().parent / "hub" / "ingest.py"
    run_path(str(receiver), run_name="__main__")
