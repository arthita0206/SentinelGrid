# Lambda Functions

Will hold the AWS Lambda function code implementing the Monitor/Analyze/
Plan/Execute stages of the MAPE-K loop once the project moves into the
cloud-integration phase.

## anomaly_detector/ (current)

A local, deterministic, AWS-independent fault detector (`detector.py`)
plus a thin Lambda adapter around it (`handler.py`) — the prototype for
what will eventually run as the Analyze stage. See
[anomaly_detector/README.md](anomaly_detector/README.md) for details,
including supported event shapes and how to invoke it locally.

**No Lambda function is deployed to AWS, and no IoT Rule invokes it.**
`lambda_handler` is plain, testable Python — it can be called directly in
tests or locally, but nothing in AWS calls it yet. The Lambda deployment
itself, its IoT Rule/trigger wiring, and Step Functions integration all
remain future steps.
