#!/usr/bin/env bash
# Deploys SentinelGrid's anomaly-events-storage CloudFormation stack:
#   AWS::DynamoDB::Table (SentinelGridAnomalyEvents)
#   (infrastructure/anomaly-events-storage.yaml)
#
# This script reads NO credentials and embeds NONE. It relies entirely on
# the AWS CLI's normal profile/credential resolution (e.g. a profile set
# up via `aws configure` or `aws login`).
#
# This template creates no IAM resources, so no --capabilities flag is
# needed. It also does NOT grant the anomaly detector Lambda's execution
# role permission to write to this table, and does NOT touch the existing
# telemetry table, IoT rules, or the anomaly-detector Lambda stack in any
# way — see infrastructure/README.md.
#
# Usage:
#   ./infrastructure/deploy-anomaly-events-storage.sh
#
# Override the profile/region if needed:
#   AWS_PROFILE=my-profile AWS_REGION=us-east-1 ./infrastructure/deploy-anomaly-events-storage.sh
#
# This script is NOT invoked automatically by anything in this repo — run
# it yourself, deliberately, when you're ready to deploy. It has not been
# executed as part of writing it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/anomaly-events-storage.yaml"

STACK_NAME="${STACK_NAME:-sentinelgrid-anomaly-events-storage}"
AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"
AWS_REGION="${AWS_REGION:-ap-south-1}"

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
