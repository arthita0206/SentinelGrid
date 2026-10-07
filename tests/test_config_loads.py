"""Sanity check that the local project foundation is wired up correctly."""

import yaml

from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "simulation_config.yaml"


def test_config_file_exists():
    assert CONFIG_PATH.exists()


def test_config_file_is_valid_yaml():
    with open(CONFIG_PATH, "r") as f:
        config = yaml.safe_load(f)

    assert isinstance(config, dict)
    assert "power" in config
    assert "water" in config
    assert "traffic" in config
    assert "telemetry" in config
    assert "fault_injection" in config
    assert "publisher" in config
    assert "aws" in config


def test_publisher_defaults_to_local():
    with open(CONFIG_PATH, "r") as f:
        config = yaml.safe_load(f)

    assert config["publisher"]["mode"] == "local"


def test_aws_config_has_expected_defaults_and_no_credentials():
    with open(CONFIG_PATH, "r") as f:
        config = yaml.safe_load(f)

    aws_config = config["aws"]
    assert aws_config["region"] == "ap-south-1"
    assert aws_config["profile"] == "sentinelgrid"
    assert aws_config["iot"]["topic"]

    forbidden_substrings = ("secret", "access_key", "access-key", "token", "password")
    serialized = str(aws_config).lower()
    for forbidden in forbidden_substrings:
        assert forbidden not in serialized
