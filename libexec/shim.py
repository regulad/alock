"""Entry point for bin/tofu, bin/terraform and bin/ansible-playbook."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alock.apply import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
