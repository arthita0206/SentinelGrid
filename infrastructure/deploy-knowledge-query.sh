#!/usr/bin/env bash
# Deploys SentinelGrid's knowledge-query CloudFormation stack:
#   AWS::Lambda::Function (SentinelGridKnowledgeQuery) + its execution role
#   + AWS::ApiGatewayV2::Api/Integration/Route(x4)/Stage/Lambda::Permission
#   (infrastructure/knowledge-query.yaml)
#
# This script embeds and prints NO credentials -- it relies entirely on the
# AWS CLI's normal profile/credential resolution (e.g. a profile set up
# via `aws configure` or `aws login`).
#
# REQUIRES an EXISTING, VERSIONING-ENABLED S3 bucket you control, passed
# via DEPLOYMENT_BUCKET (the same bucket already used by
# deploy-anomaly-detector.sh / deploy-remediation-lambdas.sh). This script
# does NOT create that bucket and does NOT enable versioning on it -- it
# verifies both and fails clearly if either is missing.
#
# REQUIRES infrastructure/anomaly-events-storage.yaml's
# SentinelGridAnomalyEventsTableName export (added in Step 35 -- see that
# template's own comments) to already be deployed, in addition to the
# pre-existing SentinelGridNodeState*/SentinelGridRecoveryEvents* exports
# from earlier steps -- this script does not deploy or modify any of
# those storage stacks itself; it only reads their exports via
# CloudFormation ImportValue at stack-deploy time.
#
# Usage:
#   DEPLOYMENT_BUCKET=my-existing-versioned-bucket ./infrastructure/deploy-knowledge-query.sh
#
# Override profile/region/package key/stack name if needed:
#   AWS_PROFILE=my-profile AWS_REGION=us-east-1 \
#   DEPLOYMENT_PACKAGE_KEY=custom/key.zip \
#   DEPLOYMENT_BUCKET=my-existing-versioned-bucket \
#     ./infrastructure/deploy-knowledge-query.sh
#
# This script is NOT invoked automatically by anything in this repo and is
# not run by the test suite -- run it yourself, deliberately, when you're
# ready to deploy. It has not been executed as part of writing it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/knowledge-query.yaml"
PACKAGE_SCRIPT="${SCRIPT_DIR}/package-knowledge-query.sh"
PACKAGE_ZIP="${SCRIPT_DIR}/build/knowledge-query.zip"

STACK_NAME="${STACK_NAME:-sentinelgrid-knowledge-query}"
AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"
AWS_REGION="${AWS_REGION:-ap-south-1}"
DEPLOYMENT_PACKAGE_KEY="${DEPLOYMENT_PACKAGE_KEY:-sentinelgrid/knowledge-query.zip}"

if [[ -z "${DEPLOYMENT_BUCKET:-}" ]]; then
  echo "ERROR: DEPLOYMENT_BUCKET is not set." >&2
  echo "This script does not create an S3 bucket for you -- set DEPLOYMENT_BUCKET" >&2
  echo "to an existing, versioning-enabled bucket you control. Example:" >&2
  echo "  DEPLOYMENT_BUCKET=my-existing-versioned-bucket $0" >&2
  exit 1
fi

echo "Verifying AWS identity (profile: ${AWS_PROFILE})..."
aws sts get-caller-identity --profile "${AWS_PROFILE}" --region "${AWS_REGION}"

echo ""
echo "Verifying S3 bucket '${DEPLOYMENT_BUCKET}' exists (profile: ${AWS_PROFILE}, region: ${AWS_REGION})..."
if ! aws s3api head-bucket \
      --bucket "${DEPLOYMENT_BUCKET}" \
      --profile "${AWS_PROFILE}" \
      --region "${AWS_REGION}" 2>/dev/null; then
  echo "ERROR: S3 bucket '${DEPLOYMENT_BUCKET}' does not exist, or is not" >&2
  echo "accessible with profile '${AWS_PROFILE}'. This script does not" >&2
  echo "create a bucket for you -- see infrastructure/README.md." >&2
  exit 1
fi

echo "Verifying versioning is enabled on '${DEPLOYMENT_BUCKET}'..."
VERSIONING_STATUS="$(aws s3api get-bucket-versioning \
  --bucket "${DEPLOYMENT_BUCKET}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "Status" \
  --output text)"

if [[ "${VERSIONING_STATUS}" != "Enabled" ]]; then
  echo "ERROR: S3 versioning is not enabled on bucket '${DEPLOYMENT_BUCKET}'" >&2
  echo "(current status: '${VERSIONING_STATUS}')." >&2
  exit 1
fi

echo ""
echo "Validating CloudFormation template ${TEMPLATE_FILE}..."
aws cloudformation validate-template \
  --template-body "file://${TEMPLATE_FILE}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}"

echo ""
echo "Packaging the knowledge-query Lambda..."
"${PACKAGE_SCRIPT}" "${PACKAGE_ZIP}"

echo ""
echo "Uploading package to s3://${DEPLOYMENT_BUCKET}/${DEPLOYMENT_PACKAGE_KEY}..."
aws s3 cp "${PACKAGE_ZIP}" "s3://${DEPLOYMENT_BUCKET}/${DEPLOYMENT_PACKAGE_KEY}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}"

PACKAGE_VERSION_ID="$(aws s3api head-object \
  --bucket "${DEPLOYMENT_BUCKET}" \
  --key "${DEPLOYMENT_PACKAGE_KEY}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "VersionId" \
  --output text)"

if [[ -z "${PACKAGE_VERSION_ID}" || "${PACKAGE_VERSION_ID}" == "None" ]]; then
  echo "ERROR: could not obtain an S3 VersionId for the object just uploaded" >&2
  echo "to s3://${DEPLOYMENT_BUCKET}/${DEPLOYMENT_PACKAGE_KEY}." >&2
  exit 1
fi

echo "Uploaded object version: ${PACKAGE_VERSION_ID}"

echo ""
echo "Deploying stack '${STACK_NAME}' to region '${AWS_REGION}' using profile '${AWS_PROFILE}'..."
aws cloudformation deploy \
  --stack-name "${STACK_NAME}" \
  --template-file "${TEMPLATE_FILE}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      "DeploymentBucketName=${DEPLOYMENT_BUCKET}" \
      "DeploymentPackageKey=${DEPLOYMENT_PACKAGE_KEY}" \
      "DeploymentPackageVersion=${PACKAGE_VERSION_ID}" \
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
echo "Deployed Lambda code from s3://${DEPLOYMENT_BUCKET}/${DEPLOYMENT_PACKAGE_KEY}"
echo "(version: ${PACKAGE_VERSION_ID})."
