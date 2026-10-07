#!/usr/bin/env bash
# Deploys SentinelGrid's remediation-lambdas CloudFormation stack:
#   3x AWS::Lambda::Function (SentinelGridRemediationPlanner/Executor/Verifier)
#   + 3x dedicated AWS::IAM::Role (infrastructure/remediation-lambdas.yaml)
#
# This script embeds and prints NO credentials — it relies entirely on the
# AWS CLI's normal profile/credential resolution.
#
# REQUIRES the EXISTING, VERSIONING-ENABLED S3 deployment bucket already
# used by deploy-anomaly-detector.sh (DEPLOYMENT_BUCKET) — this script
# does NOT create a bucket. Each of the three packages is pinned to its
# own immutable S3 object VersionId (see package-remediation-lambdas.sh
# and infrastructure/README.md's "Why an immutable S3 object version, not
# just bucket/key" section for why this matters).
#
# Usage:
#   DEPLOYMENT_BUCKET=sentinelgrid-lambda-deploy-401429382000 ./infrastructure/deploy-remediation-lambdas.sh
#
# Override profile/region/stack name if needed via AWS_PROFILE/AWS_REGION/STACK_NAME.
#
# This script is NOT invoked automatically by anything in this repo and is
# not run by the test suite — run it yourself, deliberately, when you're
# ready to deploy.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/remediation-lambdas.yaml"
PACKAGE_SCRIPT="${SCRIPT_DIR}/package-remediation-lambdas.sh"
BUILD_DIR="${SCRIPT_DIR}/build"

STACK_NAME="${STACK_NAME:-sentinelgrid-remediation-lambdas}"
AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"
AWS_REGION="${AWS_REGION:-ap-south-1}"

PLANNER_KEY="${PLANNER_KEY:-sentinelgrid/remediation-planner.zip}"
EXECUTOR_KEY="${EXECUTOR_KEY:-sentinelgrid/remediation-executor.zip}"
VERIFIER_KEY="${VERIFIER_KEY:-sentinelgrid/remediation-verifier.zip}"

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
echo "Validating CloudFormation template ${TEMPLATE_FILE}..."
aws cloudformation validate-template \
  --template-body "file://${TEMPLATE_FILE}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}"

echo ""
echo "Packaging the three remediation Lambdas..."
"${PACKAGE_SCRIPT}" "${BUILD_DIR}"

declare -A PACKAGE_KEY=(
  [planner]="${PLANNER_KEY}"
  [executor]="${EXECUTOR_KEY}"
  [verifier]="${VERIFIER_KEY}"
)
declare -A PACKAGE_VERSION

for name in planner executor verifier; do
  zip_path="${BUILD_DIR}/remediation-${name}.zip"
  key="${PACKAGE_KEY[${name}]}"

  echo ""
  echo "Uploading ${name} package to s3://${DEPLOYMENT_BUCKET}/${key}..."
  aws s3 cp "${zip_path}" "s3://${DEPLOYMENT_BUCKET}/${key}" \
    --profile "${AWS_PROFILE}" \
    --region "${AWS_REGION}"

  version_id="$(aws s3api head-object \
    --bucket "${DEPLOYMENT_BUCKET}" \
    --key "${key}" \
    --profile "${AWS_PROFILE}" \
    --region "${AWS_REGION}" \
    --query "VersionId" \
    --output text)"

  if [[ -z "${version_id}" || "${version_id}" == "None" ]]; then
    echo "ERROR: could not obtain an S3 VersionId for ${name} at s3://${DEPLOYMENT_BUCKET}/${key}." >&2
    exit 1
  fi

  echo "Uploaded ${name} object version: ${version_id}"
  PACKAGE_VERSION[${name}]="${version_id}"
done

echo ""
echo "Deploying stack '${STACK_NAME}' to region '${AWS_REGION}' using profile '${AWS_PROFILE}'..."
aws cloudformation deploy \
  --stack-name "${STACK_NAME}" \
  --template-file "${TEMPLATE_FILE}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      "DeploymentBucketName=${DEPLOYMENT_BUCKET}" \
      "PlannerPackageKey=${PACKAGE_KEY[planner]}" \
      "PlannerPackageVersion=${PACKAGE_VERSION[planner]}" \
      "ExecutorPackageKey=${PACKAGE_KEY[executor]}" \
      "ExecutorPackageVersion=${PACKAGE_VERSION[executor]}" \
      "VerifierPackageKey=${PACKAGE_KEY[verifier]}" \
      "VerifierPackageVersion=${PACKAGE_VERSION[verifier]}" \
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
for fn in SentinelGridRemediationPlanner SentinelGridRemediationExecutor SentinelGridRemediationVerifier; do
  echo ""
  echo "--- ${fn} ---"
  aws lambda get-function-configuration \
    --function-name "${fn}" \
    --profile "${AWS_PROFILE}" \
    --region "${AWS_REGION}" \
    --query "{Runtime:Runtime,Architectures:Architectures,MemorySize:MemorySize,Timeout:Timeout,Handler:Handler,Role:Role,LastUpdateStatus:LastUpdateStatus,State:State}" \
    --output table
done

echo ""
echo "Deployment complete. Package versions used:"
echo "  planner:  ${PACKAGE_VERSION[planner]}"
echo "  executor: ${PACKAGE_VERSION[executor]}"
echo "  verifier: ${PACKAGE_VERSION[verifier]}"
