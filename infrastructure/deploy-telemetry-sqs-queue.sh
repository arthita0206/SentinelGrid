#!/usr/bin/env bash
# Deploys SentinelGrid's SQS telemetry ingestion stack:
#   AWS::SQS::Queue (SentinelGridTelemetryQueue + SentinelGridTelemetryDLQ)
#   + a new IoT topic rule that additionally routes sentinelgrid/telemetry
#   into the main queue, via a dedicated IAM role
#   (infrastructure/telemetry-sqs-queue.yaml).
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
#   ./infrastructure/deploy-telemetry-sqs-queue.sh
#
# Override profile/region/stack name/queue settings if needed via
# AWS_PROFILE/AWS_REGION/STACK_NAME/MESSAGE_RETENTION_PERIOD_SECONDS/
# VISIBILITY_TIMEOUT_SECONDS/MAX_RECEIVE_COUNT.
#
# This script does not retry a failed deployment — if `aws cloudformation
# deploy` fails, it exits with the AWS CLI's own error and stops.
#
# This script is NOT invoked automatically by anything in this repo and
# is not run by the test suite — run it yourself, deliberately, when
# you're ready to deploy.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/telemetry-sqs-queue.yaml"

STACK_NAME="${STACK_NAME:-sentinelgrid-telemetry-sqs-queue}"
AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"
AWS_REGION="${AWS_REGION:-ap-south-1}"
MESSAGE_RETENTION_PERIOD_SECONDS="${MESSAGE_RETENTION_PERIOD_SECONDS:-345600}"
VISIBILITY_TIMEOUT_SECONDS="${VISIBILITY_TIMEOUT_SECONDS:-90}"
MAX_RECEIVE_COUNT="${MAX_RECEIVE_COUNT:-5}"

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
      "MessageRetentionPeriodSeconds=${MESSAGE_RETENTION_PERIOD_SECONDS}" \
      "VisibilityTimeoutSeconds=${VISIBILITY_TIMEOUT_SECONDS}" \
      "MaxReceiveCount=${MAX_RECEIVE_COUNT}" \
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
