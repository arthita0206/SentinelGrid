#!/usr/bin/env bash
# Deploys SentinelGrid's anomaly-detector CloudFormation stack:
#   AWS::Lambda::Function (SentinelGridAnomalyDetector) + its execution role
#   (infrastructure/anomaly-detector.yaml)
#
# This script embeds and prints NO credentials — it relies entirely on the
# AWS CLI's normal profile/credential resolution (e.g. a profile set up
# via `aws configure` or `aws login`).
#
# REQUIRES an EXISTING, VERSIONING-ENABLED S3 bucket you control, passed
# via DEPLOYMENT_BUCKET. This script does NOT create that bucket and does
# NOT enable versioning on it — it verifies both and fails clearly if
# either is missing, rather than silently deploying an unversioned
# object. See infrastructure/README.md for why an immutable object
# version is required: overwriting the same S3 key in place does not, by
# itself, give CloudFormation anything that looks different, so
# `cloudformation deploy` can report "No changes to deploy" even though
# the Lambda code actually changed. Pinning to the upload's S3 VersionId
# (via DeploymentPackageVersion) fixes that.
#
# Usage:
#   DEPLOYMENT_BUCKET=my-existing-versioned-bucket ./infrastructure/deploy-anomaly-detector.sh
#
# Override profile/region/package key/stack name if needed:
#   AWS_PROFILE=my-profile AWS_REGION=us-east-1 \
#   DEPLOYMENT_PACKAGE_KEY=custom/key.zip \
#   DEPLOYMENT_BUCKET=my-existing-versioned-bucket \
#     ./infrastructure/deploy-anomaly-detector.sh
#
# This script is NOT invoked automatically by anything in this repo and is
# not run by the test suite — run it yourself, deliberately, when you're
# ready to deploy. It has not been executed as part of writing it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/anomaly-detector.yaml"
PACKAGE_SCRIPT="${SCRIPT_DIR}/package-anomaly-detector.sh"
PACKAGE_ZIP="${SCRIPT_DIR}/build/anomaly-detector.zip"

STACK_NAME="${STACK_NAME:-sentinelgrid-anomaly-detector}"
TRIGGER_STACK_NAME="${TRIGGER_STACK_NAME:-sentinelgrid-remediation-trigger}"
AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"
AWS_REGION="${AWS_REGION:-ap-south-1}"
DEPLOYMENT_PACKAGE_KEY="${DEPLOYMENT_PACKAGE_KEY:-sentinelgrid/anomaly-detector.zip}"

if [[ -z "${DEPLOYMENT_BUCKET:-}" ]]; then
  echo "ERROR: DEPLOYMENT_BUCKET is not set." >&2
  echo "This script does not create an S3 bucket for you — set DEPLOYMENT_BUCKET" >&2
  echo "to an existing, versioning-enabled bucket you control. Example:" >&2
  echo "  DEPLOYMENT_BUCKET=my-existing-versioned-bucket $0" >&2
  exit 1
fi

echo "Verifying S3 bucket '${DEPLOYMENT_BUCKET}' exists (profile: ${AWS_PROFILE}, region: ${AWS_REGION})..."
if ! aws s3api head-bucket \
      --bucket "${DEPLOYMENT_BUCKET}" \
      --profile "${AWS_PROFILE}" \
      --region "${AWS_REGION}" 2>/dev/null; then
  echo "ERROR: S3 bucket '${DEPLOYMENT_BUCKET}' does not exist, or is not" >&2
  echo "accessible with profile '${AWS_PROFILE}'. This script does not" >&2
  echo "create a bucket for you — see infrastructure/README.md." >&2
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
  echo "An immutable S3 object VersionId is required so CloudFormation" >&2
  echo "reliably detects a Lambda code change when the S3 key is reused —" >&2
  echo "without versioning, redeploying the same key can silently report" >&2
  echo "'No changes to deploy' even though the package content changed." >&2
  echo "This script does NOT enable versioning for you. To enable it:" >&2
  echo "  aws s3api put-bucket-versioning --bucket ${DEPLOYMENT_BUCKET} \\" >&2
  echo "    --versioning-configuration Status=Enabled \\" >&2
  echo "    --profile ${AWS_PROFILE} --region ${AWS_REGION}" >&2
  exit 1
fi

echo "Reading the existing remediation trigger Lambda ARN from stack '${TRIGGER_STACK_NAME}' (read-only)..."
TRIGGER_FUNCTION_ARN="$(aws cloudformation describe-stacks \
  --stack-name "${TRIGGER_STACK_NAME}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='FunctionArn'].OutputValue" \
  --output text)"

if [[ -z "${TRIGGER_FUNCTION_ARN}" || "${TRIGGER_FUNCTION_ARN}" == "None" ]]; then
  echo "ERROR: could not read FunctionArn from stack '${TRIGGER_STACK_NAME}'." >&2
  echo "This script does not invent or guess this ARN — verify the stack" >&2
  echo "exists and has a FunctionArn Output before retrying." >&2
  exit 1
fi
echo "Remediation trigger Lambda ARN: ${TRIGGER_FUNCTION_ARN}"

echo ""
echo "Packaging the anomaly detector..."
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
  echo "This should not happen once versioning is confirmed Enabled above —" >&2
  echo "double-check the bucket's versioning configuration and try again." >&2
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
      "RemediationTriggerFunctionArn=${TRIGGER_FUNCTION_ARN}" \
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
