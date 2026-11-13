from .cli import main
from .core import APIError
import sys

try:
    raise SystemExit(main())
except (APIError, OSError, RuntimeError, ValueError) as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(1)
