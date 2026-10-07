#!/usr/bin/env bash
# Deploys SentinelGrid's anomaly-detection-rule CloudFormation stack:
#   AWS::IoT::TopicRule (sentinelgrid_telemetry_to_lambda) +
#   AWS::Lambda::Permission letting it invoke SentinelGridAnomalyDetector
#   (infrastructure/anomaly-detection-rule.yaml)
#
# This script embeds and prints NO credentials — it relies entirely on the
# AWS CLI's normal profile/credential resolution.
#
# It does NOT touch the existing sentinelgrid_telemetry_to_dynamodb stack
# or the anomaly-detector Lambda stack in any way — this is a separate,
# independent stack.
#
# The anomaly detector Lambda (SentinelGridAnomalyDetector) must already
# be deployed (see infrastructure/anomaly-detector.yaml /
# deploy-anomaly-detector.sh) before this stack can be deployed — this
# script does not create or package a Lambda function itself, so there is
# no packaging step here.
#
# Usage:
#   ./infrastructure/deploy-anomaly-detection-rule.sh
#
# Override profile/region/function name/stack name if needed:
#   AWS_PROFILE=my-profile AWS_REGION=us-east-1 \
#   ANOMALY_DETECTOR_FUNCTION_NAME=SomeOtherName \
#     ./infrastructure/deploy-anomaly-detection-rule.sh
#
# This script is NOT invoked automatically by anything in this repo and is
# not run by the test suite — run it yourself, deliberately, when you're
# ready to deploy. It has not been executed as part of writing it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/anomaly-detection-rule.yaml"

STACK_NAME="${STACK_NAME:-sentinelgrid-anomaly-detection-rule}"
AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"
AWS_REGION="${AWS_REGION:-ap-south-1}"
ANOMALY_DETECTOR_FUNCTION_NAME="${ANOMALY_DETECTOR_FUNCTION_NAME:-SentinelGridAnomalyDetector}"

if [[ ! -f "${TEMPLATE_FILE}" ]]; then
  echo "ERROR: template not found at ${TEMPLATE_FILE}" >&2
  exit 1
fi

echo "Deploying stack '${STACK_NAME}' to region '${AWS_REGION}' using profile '${AWS_PROFILE}'..."
echo "This will invoke Lambda function: ${ANOMALY_DETECTOR_FUNCTION_NAME} (must already exist)."

aws cloudformation deploy \
  --stack-name "${STACK_NAME}" \
  --template-file "${TEMPLATE_FILE}" \
  --parameter-overrides \
      "AnomalyDetectorFunctionName=${ANOMALY_DETECTOR_FUNCTION_NAME}" \
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

echo ""
echo "NOTE: the existing sentinelgrid_telemetry_to_dynamodb rule and the"
echo "SentinelGridAnomalyDetector Lambda function/execution role are"
echo "untouched by this stack — it only adds a new, independent IoT Rule"
echo "and a Lambda resource-based permission."
