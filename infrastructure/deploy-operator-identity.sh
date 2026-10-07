#!/usr/bin/env bash
# Deploys SentinelGrid's dedicated operator-identity CloudFormation stack
# (Step 48): AWS::IAM::Role SentinelGridOperatorRole only
# (infrastructure/operator-identity.yaml). No packaging, no S3 upload —
# this stack creates exactly one IAM role and nothing else.
#
# This script embeds and prints NO credentials — it relies entirely on
# the AWS CLI's normal profile/credential resolution. It is run under
# the CURRENT identity (today: account root, per Step 47's finding) —
# creating the first non-root identity in an account unavoidably
# requires an already-privileged identity to do it once.
#
# Usage:
#   ./infrastructure/deploy-operator-identity.sh
#
# Override profile/region/stack name if needed via AWS_PROFILE/AWS_REGION/STACK_NAME.
#
# This script is NOT invoked automatically by anything in this repo and
# is not run by the test suite — run it yourself, deliberately, when
# you're ready to deploy.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/operator-identity.yaml"

STACK_NAME="${STACK_NAME:-sentinelgrid-operator-identity}"
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
