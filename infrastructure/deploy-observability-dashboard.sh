#!/usr/bin/env bash
# Deploys SentinelGrid's CloudWatch observability dashboard stack:
#   AWS::CloudWatch::Dashboard (SentinelGrid-Observability)
#   (infrastructure/observability-dashboard.yaml)
#
# This script reads NO credentials and embeds NONE. It relies entirely on
# the AWS CLI's normal profile/credential resolution.
#
# This template creates no IAM resources, no alarms, no SNS topics, and
# no new metrics -- it only visualizes the existing SentinelGrid
# namespace custom metrics that lambda_functions/anomaly_detector/
# handler.py and lambda_functions/remediation/executor.py already
# publish (see shared/metrics.py, Step 28). No --capabilities flag is
# needed. This stack does not modify any other stack, any Lambda, any
# IoT rule, the SQS queue, or the Step Functions state machine.
#
# Usage:
#   ./infrastructure/deploy-observability-dashboard.sh
#
# Override the profile/region if needed:
#   AWS_PROFILE=my-profile AWS_REGION=us-east-1 ./infrastructure/deploy-observability-dashboard.sh
#
# This script is NOT invoked automatically by anything in this repo — run
# it yourself, deliberately, when you're ready to deploy. It has not been
# executed as part of writing it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/observability-dashboard.yaml"

STACK_NAME="${STACK_NAME:-sentinelgrid-observability-dashboard}"
AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"
AWS_REGION="${AWS_REGION:-ap-south-1}"

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
aws cloudformation deploy \
  --stack-name "${STACK_NAME}" \
  --template-file "${TEMPLATE_FILE}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}"

echo ""
echo "Stack outputs:"
aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs" \
  --output table
