"""Loads SentinelGrid's local YAML configuration."""

from pathlib import Path
from typing import Optional, Union
import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "simulation_config.yaml"


def load_config(path: Optional[Union[str, Path]] = None) -> dict:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(config_path, "r") as f:
        return yaml.safe_load(f)
