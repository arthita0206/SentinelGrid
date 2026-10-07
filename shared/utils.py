"""Small helpers shared across node telemetry generators."""


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
