"""Entry point: python -m simulator

Publisher selection is controlled by config/simulation_config.yaml's
`publisher.mode` (local | aws_iot | both). AWS IoT support is only
imported when actually needed, so local-only runs never require boto3 or
an AWS profile to be configured.
"""

from shared.config import load_config
from simulator.city_simulator import CitySimulator
from simulator.publisher import LocalPublisher, MultiPublisher, Publisher

VALID_MODES = ("local", "aws_iot", "both")


def _build_aws_iot_publisher(config: dict):
    from simulator.aws_publisher import AwsIotPublisher  # deferred: needs boto3 only in AWS modes

    aws_config = config.get("aws") or {}
    iot_config = aws_config.get("iot") or {}
    return AwsIotPublisher(
        region=aws_config.get("region"),
        topic=iot_config.get("topic"),
        profile=aws_config.get("profile"),
        endpoint_url=iot_config.get("endpoint_url"),
    )


def build_publisher(config: dict) -> Publisher:
    mode = (config.get("publisher") or {}).get("mode", "local")

    if mode == "local":
        return LocalPublisher()
    if mode == "aws_iot":
        return _build_aws_iot_publisher(config)
    if mode == "both":
        return MultiPublisher([LocalPublisher(), _build_aws_iot_publisher(config)])

    raise ValueError(f"Unknown publisher mode {mode!r}; expected one of {VALID_MODES}")


def main() -> None:
    config = load_config()
    publisher = build_publisher(config)
    mode = (config.get("publisher") or {}).get("mode", "local")

    print(f"Starting SentinelGrid local simulator (Ctrl+C to stop)... [publisher: {mode}]")
    simulator = CitySimulator(config, publisher)
    simulator.run()


if __name__ == "__main__":
    main()
