#!/usr/bin/env bash
# Deploys SentinelGrid's telemetry-storage CloudFormation stack:
#   DynamoDB table + IoT Topic Rule + IAM role (infrastructure/telemetry-storage.yaml)
#
# This script reads NO credentials and embeds NONE. It relies entirely on
# the AWS CLI's normal profile/credential resolution (e.g. a profile set
# up via `aws configure` or `aws login`).
#
# Usage:
#   ./infrastructure/deploy-telemetry-storage.sh
#
# Override the profile/region if needed:
#   AWS_PROFILE=my-profile AWS_REGION=us-east-1 ./infrastructure/deploy-telemetry-storage.sh
#
# This script is NOT invoked automatically by anything in this repo — run
# it yourself, deliberately, when you're ready to deploy.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/telemetry-storage.yaml"

STACK_NAME="${STACK_NAME:-sentinelgrid-telemetry-storage}"
AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"
AWS_REGION="${AWS_REGION:-ap-south-1}"

echo "Deploying stack '${STACK_NAME}' to region '${AWS_REGION}' using profile '${AWS_PROFILE}'..."

aws cloudformation deploy \
  --stack-name "${STACK_NAME}" \
  --template-file "${TEMPLATE_FILE}" \
  --capabilities CAPABILITY_NAMED_IAM \
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
