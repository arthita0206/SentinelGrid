# Simulator

Local, mostly cloud-agnostic telemetry simulation for SentinelGrid's three
smart-city subsystems (power, water, traffic). Node generation and fault
handling have no AWS dependencies; `aws_publisher.py` is the one optional
exception (Step 4), used only when AWS IoT publishing is selected.

```
CitySimulator -> Telemetry objects -> Publisher interface
                                           |-- LocalPublisher (default)
                                           |-- AwsIotPublisher (Step 4, opt-in)
```

## Layout

- `node.py` — `BaseNode`: generic virtual node abstraction (node_id,
  subsystem, status, `generate_telemetry()`).
- `subsystems/power.py` — `PowerNode` (load, voltage, frequency)
- `subsystems/water.py` — `WaterNode` (pressure, flow_rate, reservoir_level)
- `subsystems/traffic.py` — `TrafficNode` (vehicle_count, average_speed, signal_state)
- `publisher.py` — `Publisher` interface, `LocalPublisher` (stdout), and
  `MultiPublisher` (fans out to several publishers, used by `both` mode)
- `aws_publisher.py` — `AwsIotPublisher` (Step 4): publishes to AWS IoT
  Core. Only imported when `publisher.mode` is `aws_iot`/`both`, so local
  runs never require `boto3` or an AWS profile.
- `city_simulator.py` — `CitySimulator`: loads config, builds nodes, drives
  the telemetry loop at the configured interval
- `__main__.py` — entry point for `python -m simulator`; also selects the
  publisher from config via `build_publisher()`

## Running

```bash
python -m simulator
```

Defaults to `LocalPublisher`: prints one JSON telemetry line per node per
tick to stdout. Stop with Ctrl+C. See the top-level
[README.md](../README.md) for telemetry field details, fault injection,
and how to switch to AWS IoT publishing.
