"""Entry point for the /alock:retrofit command's shell injection."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alock.retrofit import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
