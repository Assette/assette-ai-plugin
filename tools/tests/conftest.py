"""Put the analysis tools dir on sys.path so tests can `import plan_build` directly.

The analysis CLIs use sibling-module imports (e.g. `from _inventory_common import now_iso`),
which resolve when the tools dir is sys.path[0] — true when a CLI is run as a script, and
arranged here for the test process.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
