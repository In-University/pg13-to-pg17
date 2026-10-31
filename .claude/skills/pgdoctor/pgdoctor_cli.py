"""Run pgdoctor from anywhere:  python <path-to-skill>/pgdoctor_cli.py analyze ..."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pgdoctor.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
