"""Allow `python3 -m sesh`."""

import sys

from .cli import main

sys.exit(main())
