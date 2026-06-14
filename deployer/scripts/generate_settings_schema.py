import sys
from pathlib import Path


sys.path.append(str(Path(__file__).parents[1]))
from config_schema import Settings  # noqa: E402


Settings.save_schema(Path(__file__).parents[1] / "settings.schema.yaml")
