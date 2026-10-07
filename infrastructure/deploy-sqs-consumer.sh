#!/usr/bin/env bash
# Deploys SentinelGrid's SQS consumer CloudFormation stack (Step 26):
#   AWS::Lambda::Function (SentinelGridSqsTelemetryConsumer) + its
#   execution role + an AWS::Lambda::EventSourceMapping from the
#   existing SentinelGridTelemetryQueue
#   (infrastructure/sqs-consumer.yaml)
#
# This script embeds and prints NO credentials — it relies entirely on
# the AWS CLI's normal profile/credential resolution.
#
# REQUIRES an EXISTING, VERSIONING-ENABLED S3 bucket you control, passed
# via DEPLOYMENT_BUCKET — same requirement as
# infrastructure/deploy-anomaly-detector.sh, for the same reason (see
# that script's own header comment).
#
# This script does NOT deploy the anomaly-detector stack, the SQS queue
# stack, or touch the direct IoT -> Analyzer rule
# (infrastructure/anomaly-detection-rule.yaml) — it only reads the two
# existing stacks' live Outputs and deploys this one, new, additive
# stack. Disabling the direct IoT rule (the Step 26 cutover) is a
# separate, deliberate step performed only after this stack is verified.
#
# Usage:
#   DEPLOYMENT_BUCKET=my-existing-versioned-bucket ./infrastructure/deploy-sqs-consumer.sh
#
# Override profile/region/package key/stack name/batch size if needed:
#   AWS_PROFILE=my-profile AWS_REGION=us-east-1 \
#   DEPLOYMENT_PACKAGE_KEY=custom/key.zip \
#   BATCH_SIZE=5 \
#   DEPLOYMENT_BUCKET=my-existing-versioned-bucket \
#     ./infrastructure/deploy-sqs-consumer.sh
#
# This script is NOT invoked automatically by anything in this repo and
# is not run by the test suite — run it yourself, deliberately, when
# you're ready to deploy. This script does not retry a failed
# deployment.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/sqs-consumer.yaml"
PACKAGE_SCRIPT="${SCRIPT_DIR}/package-sqs-consumer.sh"
PACKAGE_ZIP="${SCRIPT_DIR}/build/sqs-consumer.zip"

STACK_NAME="${STACK_NAME:-sentinelgrid-sqs-consumer}"
ANOMALY_DETECTOR_STACK_NAME="${ANOMALY_DETECTOR_STACK_NAME:-sentinelgrid-anomaly-detector}"
SQS_QUEUE_STACK_NAME="${SQS_QUEUE_STACK_NAME:-sentinelgrid-telemetry-sqs-queue}"
AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"
AWS_REGION="${AWS_REGION:-ap-south-1}"
DEPLOYMENT_PACKAGE_KEY="${DEPLOYMENT_PACKAGE_KEY:-sentinelgrid/sqs-consumer.zip}"
BATCH_SIZE="${BATCH_SIZE:-5}"

if [[ -z "${DEPLOYMENT_BUCKET:-}" ]]; then
  echo "ERROR: DEPLOYMENT_BUCKET is not set." >&2
  echo "This script does not create an S3 bucket for you — set DEPLOYMENT_BUCKET" >&2
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
echo "Reading the existing Analyzer Lambda ARN from stack '${ANOMALY_DETECTOR_STACK_NAME}' (read-only)..."
ANOMALY_DETECTOR_FUNCTION_ARN="$(aws cloudformation describe-stacks \
  --stack-name "${ANOMALY_DETECTOR_STACK_NAME}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='FunctionArn'].OutputValue" \
  --output text)"

if [[ -z "${ANOMALY_DETECTOR_FUNCTION_ARN}" || "${ANOMALY_DETECTOR_FUNCTION_ARN}" == "None" ]]; then
  echo "ERROR: could not read FunctionArn from stack '${ANOMALY_DETECTOR_STACK_NAME}'." >&2
  echo "This script does not invent or guess this ARN — verify the stack" >&2
  echo "exists and has a FunctionArn Output before retrying." >&2
  exit 1
fi
echo "Analyzer Lambda ARN: ${ANOMALY_DETECTOR_FUNCTION_ARN}"

echo ""
echo "Reading the existing telemetry SQS queue ARN from stack '${SQS_QUEUE_STACK_NAME}' (read-only)..."
TELEMETRY_QUEUE_ARN="$(aws cloudformation describe-stacks \
  --stack-name "${SQS_QUEUE_STACK_NAME}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='QueueArn'].OutputValue" \
  --output text)"

if [[ -z "${TELEMETRY_QUEUE_ARN}" || "${TELEMETRY_QUEUE_ARN}" == "None" ]]; then
  echo "ERROR: could not read QueueArn from stack '${SQS_QUEUE_STACK_NAME}'." >&2
  echo "This script does not invent or guess this ARN — verify the stack" >&2
  echo "exists and has a QueueArn Output before retrying." >&2
  exit 1
fi
echo "Telemetry SQS queue ARN: ${TELEMETRY_QUEUE_ARN}"

echo ""
echo "Packaging the SQS consumer..."
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
echo "Validating CloudFormation template ${TEMPLATE_FILE}..."
aws cloudformation validate-template \
  --template-body "file://${TEMPLATE_FILE}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}"

echo ""
echo "Deploying stack '${STACK_NAME}' to region '${AWS_REGION}' using profile '${AWS_PROFILE}'..."
if ! aws cloudformation deploy \
  --stack-name "${STACK_NAME}" \
  --template-file "${TEMPLATE_FILE}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      "DeploymentBucketName=${DEPLOYMENT_BUCKET}" \
      "DeploymentPackageKey=${DEPLOYMENT_PACKAGE_KEY}" \
      "DeploymentPackageVersion=${PACKAGE_VERSION_ID}" \
      "AnomalyDetectorFunctionArn=${ANOMALY_DETECTOR_FUNCTION_ARN}" \
      "TelemetryQueueArn=${TELEMETRY_QUEUE_ARN}" \
      "BatchSize=${BATCH_SIZE}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}"; then
  echo "" >&2
  echo "ERROR: deployment failed. See the CloudFormation error above/in the" >&2
  echo "console for the exact reason. This script does not retry, work" >&2
  echo "around, or invent values to make a failed deployment succeed." >&2
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
echo "Deployed Lambda code from s3://${DEPLOYMENT_BUCKET}/${DEPLOYMENT_PACKAGE_KEY}"
echo "(version: ${PACKAGE_VERSION_ID})."
echo ""
echo "NOTE: this does NOT disable the direct IoT -> Analyzer rule"
echo "(sentinelgrid_telemetry_to_lambda). That cutover is a separate,"
echo "deliberate step — verify this stack's event source mapping is"
echo "Enabled first."
