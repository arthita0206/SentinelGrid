"""SentinelGrid local analysis tooling (Step 36).

Nothing in `tools/` is deployed to AWS Lambda, referenced by any
CloudFormation template, or imported by any deployed package — it exists
purely to analyze evidence already produced by the deployed system
(CloudWatch Logs, CloudWatch metrics, Step Functions execution history)
after a live test. See `tools/performance_analysis.py` and
`docs/performance-validation.md`.
"""
