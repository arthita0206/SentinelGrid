#!/usr/bin/env bash
# Deploys SentinelGrid's remediation-trigger CloudFormation stack:
#   AWS::Lambda::Function (SentinelGridRemediationTrigger) + its
#   dedicated execution role (infrastructure/remediation-trigger.yaml)
#
# This script embeds and prints NO credentials — it relies entirely on
# the AWS CLI's normal profile/credential resolution.
#
# REQUIRES the EXISTING, VERSIONING-ENABLED S3 deployment bucket already
# used by deploy-anomaly-detector.sh/deploy-remediation-lambdas.sh
# (DEPLOYMENT_BUCKET) — this script does NOT create a bucket.
#
# The remediation state machine's ARN is NOT hardcoded: Step 18's stack
# (sentinelgrid-remediation-workflow) has no cross-stack Export for it,
# so this script reads it live from that existing, untouched stack's own
# StateMachineArn Output, then passes it as a CloudFormation parameter.
# This script never modifies that stack.
#
# Usage:
#   DEPLOYMENT_BUCKET=sentinelgrid-lambda-deploy-401429382000 ./infrastructure/deploy-remediation-trigger.sh
#
# Override profile/region/stack name if needed via AWS_PROFILE/AWS_REGION/STACK_NAME.
#
# This script is NOT invoked automatically by anything in this repo and
# is not run by the test suite — run it yourself, deliberately, when
# you're ready to deploy.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/remediation-trigger.yaml"
PACKAGE_SCRIPT="${SCRIPT_DIR}/package-remediation-trigger.sh"
PACKAGE_ZIP="${SCRIPT_DIR}/build/remediation-trigger.zip"

STACK_NAME="${STACK_NAME:-sentinelgrid-remediation-trigger}"
WORKFLOW_STACK_NAME="${WORKFLOW_STACK_NAME:-sentinelgrid-remediation-workflow}"
AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"
AWS_REGION="${AWS_REGION:-ap-south-1}"
DEPLOYMENT_PACKAGE_KEY="${DEPLOYMENT_PACKAGE_KEY:-sentinelgrid/remediation-trigger.zip}"

if [[ -z "${DEPLOYMENT_BUCKET:-}" ]]; then
  echo "ERROR: DEPLOYMENT_BUCKET is not set." >&2
  echo "This script does not create an S3 bucket for you — set DEPLOYMENT_BUCKET" >&2
  echo "to the existing, versioning-enabled deployment bucket. Example:" >&2
  echo "  DEPLOYMENT_BUCKET=sentinelgrid-lambda-deploy-401429382000 $0" >&2
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
  echo "create a bucket for you." >&2
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
echo "Reading the existing remediation workflow state machine ARN from stack '${WORKFLOW_STACK_NAME}' (read-only)..."
STATE_MACHINE_ARN="$(aws cloudformation describe-stacks \
  --stack-name "${WORKFLOW_STACK_NAME}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='StateMachineArn'].OutputValue" \
  --output text)"

if [[ -z "${STATE_MACHINE_ARN}" || "${STATE_MACHINE_ARN}" == "None" ]]; then
  echo "ERROR: could not read StateMachineArn from stack '${WORKFLOW_STACK_NAME}'." >&2
  echo "This script does not invent or guess this ARN — verify the stack" >&2
  echo "exists and has a StateMachineArn Output before retrying." >&2
  exit 1
fi
echo "State machine ARN: ${STATE_MACHINE_ARN}"

echo ""
echo "Validating CloudFormation template ${TEMPLATE_FILE}..."
aws cloudformation validate-template \
  --template-body "file://${TEMPLATE_FILE}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}"

echo ""
echo "Packaging the remediation trigger..."
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
      "RemediationStateMachineArn=${STATE_MACHINE_ARN}" \
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
echo "Verifying deployed Lambda configuration..."
aws lambda get-function-configuration \
  --function-name SentinelGridRemediationTrigger \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "{Runtime:Runtime,Architectures:Architectures,MemorySize:MemorySize,Timeout:Timeout,Handler:Handler,Role:Role,LastUpdateStatus:LastUpdateStatus,State:State,Environment:Environment}" \
  --output json

echo ""
echo "Deployed package version: ${PACKAGE_VERSION_ID}"
