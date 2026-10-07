"""Publisher abstraction: decouples telemetry generation from where it goes.

    Simulator -> Telemetry objects -> Publisher interface
                                           |-- LocalPublisher (this step)
                                           |-- AwsIotPublisher (simulator/aws_publisher.py, Step 4)
                                           |-- ConcurrentPublisher (Step 37, wraps any Publisher)
"""

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, List, Tuple

from shared.telemetry import Telemetry


class Publisher(ABC):
    @abstractmethod
    def publish(self, telemetry: Telemetry) -> None:
        """Deliver a single telemetry reading."""


class LocalPublisher(Publisher):
    """Prints telemetry as JSON to stdout. No network/AWS calls."""

    def publish(self, telemetry: Telemetry) -> None:
        print(telemetry.to_json())


class MultiPublisher(Publisher):
    """Fans one telemetry reading out to several publishers (e.g. local + AWS)."""

    def __init__(self, publishers: Iterable[Publisher]):
        self._publishers = list(publishers)

    def publish(self, telemetry: Telemetry) -> None:
        for publisher in self._publishers:
            publisher.publish(telemetry)


class ConcurrentPublisher(Publisher):
    """Wraps another `Publisher` and fans out MANY readings across a small
    thread pool (Step 37 — SRS NFR-3 scalability validation).

    Why this exists: `CitySimulator.run()`'s own per-tick loop calls
    `publisher.publish(telemetry)` once per node, sequentially -- fine at
    the default 60-node scale, but a real network call (e.g.
    `AwsIotPublisher.publish()`, empirically ~40-90ms per call) makes a
    much larger node count (e.g. 500) take far longer per tick than the
    configured `telemetry.interval_seconds` if done one at a time. This
    class does NOT change that per-tick loop or `CitySimulator` at all --
    it is an independent, reusable Publisher, used by a test driver
    (see scripts/step37_scale_test.py) that calls `publish_many()`
    directly instead of the default per-node loop, precisely to keep
    `CitySimulator`'s own default sequential behavior completely
    unchanged for every existing caller.

    `publish()` (the required abstract method) is a plain, sequential
    passthrough to the wrapped publisher -- so a `ConcurrentPublisher`
    still satisfies the `Publisher` interface and composes with
    `MultiPublisher` if ever needed, but callers that actually want
    concurrency must call `publish_many()` explicitly.

    boto3 clients (used by `AwsIotPublisher`) are safe to share and call
    concurrently across threads once constructed -- only concurrent
    *construction* of the same client has known thread-safety caveats,
    which this class avoids since the wrapped publisher's client is built
    lazily on its own first call, same as always.
    """

    def __init__(self, inner: Publisher, max_workers: int = 20):
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self._inner = inner
        self._max_workers = max_workers
        self.success_count = 0
        self.failure_count = 0
        # (node_id, error message) for every failed publish -- kept small
        # and diagnostic-only; never raised as part of publish_many()'s
        # own control flow, so one bad reading can't abort the batch.
        self.failures: List[Tuple[str, str]] = []

    def publish(self, telemetry: Telemetry) -> None:
        self._inner.publish(telemetry)

    def publish_many(self, telemetry_readings: Iterable[Telemetry]) -> dict:
        """Publishes every reading in `telemetry_readings`, fanned out
        across up to `max_workers` threads. Never raises: a failure for
        one reading is recorded in `self.failures` and does not prevent
        the rest of the batch from being attempted -- mirrors this
        project's established "one bad item must not abort the batch"
        convention (e.g. `lambda_functions/sqs_consumer/handler.py`'s
        per-record handling).

        Returns `{"success": <int>, "failure": <int>, "total": <int>}`
        for this call only (`self.success_count`/`self.failure_count`
        accumulate across every `publish_many()` call on this instance,
        for a whole test run's running total).
        """
        readings = list(telemetry_readings)
        batch_success = 0
        batch_failure = 0

        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            future_to_telemetry = {executor.submit(self._inner.publish, t): t for t in readings}
            for future in as_completed(future_to_telemetry):
                telemetry = future_to_telemetry[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 -- deliberately broad: any single publish failure must not abort the batch or crash the test driver
                    batch_failure += 1
                    self.failures.append((telemetry.node_id, f"{type(exc).__name__}: {exc}"))
                else:
                    batch_success += 1

        self.success_count += batch_success
        self.failure_count += batch_failure
        return {"success": batch_success, "failure": batch_failure, "total": len(readings)}
