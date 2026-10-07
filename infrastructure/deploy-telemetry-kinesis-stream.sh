#!/usr/bin/env bash
# Deploys SentinelGrid's Kinesis telemetry ingestion stack:
#   AWS::Kinesis::Stream (SentinelGridTelemetryStream) + a new IoT topic
#   rule that additionally routes sentinelgrid/telemetry into it,
#   partitioned by subsystem, via a dedicated IAM role
#   (infrastructure/telemetry-kinesis-stream.yaml).
#
# This script embeds and prints NO credentials — it relies entirely on
# the AWS CLI's normal profile/credential resolution.
#
# This stack is purely additive: it does not touch telemetry-storage.yaml,
# anomaly-detector.yaml, anomaly-detection-rule.yaml, or any remediation
# stack. The existing IoT -> DynamoDB and IoT -> Anomaly Detector rules
# are left running unchanged.
#
# Usage:
#   ./infrastructure/deploy-telemetry-kinesis-stream.sh
#
# Override profile/region/stack name/retention if needed via
# AWS_PROFILE/AWS_REGION/STACK_NAME/RETENTION_PERIOD_HOURS.
#
# This script is NOT invoked automatically by anything in this repo and
# is not run by the test suite — run it yourself, deliberately, when
# you're ready to deploy.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/telemetry-kinesis-stream.yaml"

STACK_NAME="${STACK_NAME:-sentinelgrid-telemetry-kinesis-stream}"
AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"
AWS_REGION="${AWS_REGION:-ap-south-1}"
RETENTION_PERIOD_HOURS="${RETENTION_PERIOD_HOURS:-24}"

echo "Verifying AWS identity (profile: ${AWS_PROFILE})..."
aws sts get-caller-identity --profile "${AWS_PROFILE}" --region "${AWS_REGION}"

echo ""
echo "Validating CloudFormation template ${TEMPLATE_FILE}..."
aws cloudformation validate-template \
  --template-body "file://${TEMPLATE_FILE}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}"

echo ""
echo "Deploying stack '${STACK_NAME}' to region '${AWS_REGION}' using profile '${AWS_PROFILE}'..."
if ! aws cloudformation deploy \
  --stack-name "${STACK_NAME}" \
  --template-file "${TEMPLATE_FILE}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      "RetentionPeriodHours=${RETENTION_PERIOD_HOURS}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}"; then
  echo "" >&2
  echo "ERROR: deployment failed. See the CloudFormation error above/in the" >&2
  echo "console for the exact reason (e.g. describe-stack-events). This" >&2
  echo "script does not retry, work around, or invent values to make a" >&2
  echo "failed deployment succeed." >&2
  exit 1
fi

echo ""
echo "Stack status:"
aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "Stacks[0].StackStatus" \
  --output text

echo ""
echo "Stack outputs:"
aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs" \
  --output table
