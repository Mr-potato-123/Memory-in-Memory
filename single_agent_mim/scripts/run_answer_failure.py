"""Run the record-only Answer Failure phase."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mim.diagnosis.runner import run_component


if __name__ == "__main__":
    raise SystemExit(run_component("answer"))
