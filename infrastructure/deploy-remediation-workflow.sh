#!/usr/bin/env bash
# Deploys SentinelGrid's remediation-workflow CloudFormation stack:
#   AWS::StepFunctions::StateMachine (SentinelGridRemediationWorkflow) +
#   its dedicated execution role (infrastructure/remediation-workflow.yaml)
#
# This script embeds and prints NO credentials — it relies entirely on the
# AWS CLI's normal profile/credential resolution (e.g. a profile set up
# via `aws configure` or `aws login`).
#
# REQUIRES an EXISTING, VERSIONING-ENABLED S3 bucket you control, passed
# via DEFINITION_BUCKET. This script does NOT create that bucket and does
# NOT enable versioning on it — it verifies both and fails clearly if
# either is missing, mirroring deploy-anomaly-detector.sh's reasoning:
# overwriting the same S3 key in place doesn't give CloudFormation
# anything that looks different, so `cloudformation deploy` can report
# "No changes to deploy" even though the definition content changed.
# Pinning to the upload's S3 VersionId (via DefinitionVersion) fixes that.
#
# IMPORTANT: this deploys the state machine EXACTLY as it exists in
# step_functions/remediation_workflow.json, uploaded byte-for-byte and
# unmodified. Its Task states still contain Step 14's unresolved
# placeholder strings (e.g. ${RemediationPlannerFunctionArn}) — no
# RemediationPlanner/RemediationExecutor/VerifyRemediation Lambda exists
# yet. This script does not substitute, invent, or otherwise resolve
# those placeholders. If AWS Step Functions rejects the definition
# because of them, this script will fail and print AWS's own error —
# it will not retry with invented values or fall back to any workaround.
#
# Usage:
#   DEFINITION_BUCKET=my-existing-versioned-bucket ./infrastructure/deploy-remediation-workflow.sh
#
# Override profile/region/key/stack name if needed:
#   AWS_PROFILE=my-profile AWS_REGION=us-east-1 \
#   DEFINITION_KEY=custom/key.json \
#   DEFINITION_BUCKET=my-existing-versioned-bucket \
#     ./infrastructure/deploy-remediation-workflow.sh
#
# This script is NOT invoked automatically by anything in this repo and is
# not run by the test suite — run it yourself, deliberately, when you're
# ready to deploy.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/remediation-workflow.yaml"
DEFINITION_FILE="${REPO_ROOT}/step_functions/remediation_workflow.json"

STACK_NAME="${STACK_NAME:-sentinelgrid-remediation-workflow}"
AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"
AWS_REGION="${AWS_REGION:-ap-south-1}"
DEFINITION_KEY="${DEFINITION_KEY:-sentinelgrid/remediation-workflow.json}"

if [[ ! -f "${DEFINITION_FILE}" ]]; then
  echo "ERROR: state-machine definition not found at ${DEFINITION_FILE}" >&2
  exit 1
fi

if [[ -z "${DEFINITION_BUCKET:-}" ]]; then
  echo "ERROR: DEFINITION_BUCKET is not set." >&2
  echo "This script does not create an S3 bucket for you — set DEFINITION_BUCKET" >&2
  echo "to an existing, versioning-enabled bucket you control. Example:" >&2
  echo "  DEFINITION_BUCKET=my-existing-versioned-bucket $0" >&2
  exit 1
fi

echo "Verifying AWS identity (profile: ${AWS_PROFILE})..."
aws sts get-caller-identity --profile "${AWS_PROFILE}" --region "${AWS_REGION}"

echo ""
echo "Verifying S3 bucket '${DEFINITION_BUCKET}' exists (profile: ${AWS_PROFILE}, region: ${AWS_REGION})..."
if ! aws s3api head-bucket \
      --bucket "${DEFINITION_BUCKET}" \
      --profile "${AWS_PROFILE}" \
      --region "${AWS_REGION}" 2>/dev/null; then
  echo "ERROR: S3 bucket '${DEFINITION_BUCKET}' does not exist, or is not" >&2
  echo "accessible with profile '${AWS_PROFILE}'. This script does not" >&2
  echo "create a bucket for you — see infrastructure/README.md." >&2
  exit 1
fi

echo "Verifying versioning is enabled on '${DEFINITION_BUCKET}'..."
VERSIONING_STATUS="$(aws s3api get-bucket-versioning \
  --bucket "${DEFINITION_BUCKET}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "Status" \
  --output text)"

if [[ "${VERSIONING_STATUS}" != "Enabled" ]]; then
  echo "ERROR: S3 versioning is not enabled on bucket '${DEFINITION_BUCKET}'" >&2
  echo "(current status: '${VERSIONING_STATUS}')." >&2
  echo "An immutable S3 object VersionId is required so CloudFormation" >&2
  echo "reliably detects a definition change when the S3 key is reused." >&2
  echo "This script does NOT enable versioning for you. To enable it:" >&2
  echo "  aws s3api put-bucket-versioning --bucket ${DEFINITION_BUCKET} \\" >&2
  echo "    --versioning-configuration Status=Enabled \\" >&2
  echo "    --profile ${AWS_PROFILE} --region ${AWS_REGION}" >&2
  exit 1
fi

echo ""
echo "Validating CloudFormation template ${TEMPLATE_FILE}..."
echo "NOTE: 'validate-template' only checks the template's own JSON/YAML"
echo "structure — it does not evaluate the embedded Step Functions"
echo "definition's Amazon States Language content. It cannot by itself"
echo "prove the state machine will be accepted; that is only known once"
echo "AWS Step Functions itself processes the definition during deploy."
aws cloudformation validate-template \
  --template-body "file://${TEMPLATE_FILE}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}"

echo ""
echo "Uploading state-machine definition to s3://${DEFINITION_BUCKET}/${DEFINITION_KEY}..."
aws s3 cp "${DEFINITION_FILE}" "s3://${DEFINITION_BUCKET}/${DEFINITION_KEY}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}"

DEFINITION_VERSION_ID="$(aws s3api head-object \
  --bucket "${DEFINITION_BUCKET}" \
  --key "${DEFINITION_KEY}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "VersionId" \
  --output text)"

if [[ -z "${DEFINITION_VERSION_ID}" || "${DEFINITION_VERSION_ID}" == "None" ]]; then
  echo "ERROR: could not obtain an S3 VersionId for the object just uploaded" >&2
  echo "to s3://${DEFINITION_BUCKET}/${DEFINITION_KEY}." >&2
  echo "This should not happen once versioning is confirmed Enabled above —" >&2
  echo "double-check the bucket's versioning configuration and try again." >&2
  exit 1
fi

echo "Uploaded object version: ${DEFINITION_VERSION_ID}"

echo ""
echo "Deploying stack '${STACK_NAME}' to region '${AWS_REGION}' using profile '${AWS_PROFILE}'..."
if ! aws cloudformation deploy \
  --stack-name "${STACK_NAME}" \
  --template-file "${TEMPLATE_FILE}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      "DefinitionBucketName=${DEFINITION_BUCKET}" \
      "DefinitionKey=${DEFINITION_KEY}" \
      "DefinitionVersion=${DEFINITION_VERSION_ID}" \
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
echo "Stack outputs:"
aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs" \
  --output table

echo ""
echo "Deployed state-machine definition from s3://${DEFINITION_BUCKET}/${DEFINITION_KEY}"
echo "(version: ${DEFINITION_VERSION_ID})."
