"""Backend configuration. Runtime paths are reserved for monitoring.

The existing CLI keeps its baseline.json default.
"""

import os
from pathlib import Path

DATA_DIRECTORY = Path(os.environ.get("RADAR_DATA_DIR", ".radar"))
DATABASE_PATH = DATA_DIRECTORY / "radar.db"
CONTRACTS_DIRECTORY = DATA_DIRECTORY / "contracts"
