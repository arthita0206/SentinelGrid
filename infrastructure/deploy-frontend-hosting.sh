#!/usr/bin/env bash
# Deploys/updates SentinelGrid's frontend static hosting (Step 45):
#   1. Deploys infrastructure/frontend-hosting.yaml (S3 bucket, CloudFront
#      distribution, Origin Access Control, bucket policy) if it doesn't
#      already exist, or applies any template changes if it does.
#   2. Syncs frontend/dist/ (the ALREADY-BUILT production bundle -- this
#      script does NOT run `npm run build` itself; build first) to the
#      private S3 bucket, with a deliberate Cache-Control split:
#        - index.html: "no-cache" -- always revalidated, so a new
#          deployment is picked up on next load rather than being served
#          stale from a browser/CDN cache.
#        - every hashed asset under dist/assets/ (JS/CSS, Vite's own
#          content-hashed filenames): "public, max-age=31536000,
#          immutable" -- safe to cache forever, because a NEW build
#          produces NEW filenames; nothing ever mutates in place.
#   3. Invalidates CloudFront for "/" and "/index.html" so the new
#      index.html (and therefore the new hashed asset references inside
#      it) is served immediately, without waiting for the (short)
#      index.html cache TTL to expire on its own.
#
# This script reads NO credentials and embeds NONE -- it relies entirely
# on the AWS CLI's normal profile/credential resolution. It is safe to
# re-run: the CloudFormation deploy is idempotent (no-op if nothing
# changed), the S3 sync only uploads changed/new objects and removes
# objects no longer present in dist/ (--delete), and CloudFront
# invalidations are idempotent in effect (repeating one is wasted cost,
# not incorrect).
#
# Usage:
#   cd frontend && npm run build && cd ..
#   ./infrastructure/deploy-frontend-hosting.sh
#
# Override the profile/region/stack/bucket if needed:
#   AWS_PROFILE=my-profile AWS_REGION=us-east-1 \
#   STACK_NAME=my-stack BUCKET_NAME=my-bucket \
#     ./infrastructure/deploy-frontend-hosting.sh
#
# This script is NOT invoked automatically by anything in this repo --
# run it yourself, deliberately, when you're ready to deploy.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/frontend-hosting.yaml"
DIST_DIR="${REPO_ROOT}/frontend/dist"

STACK_NAME="${STACK_NAME:-sentinelgrid-frontend-hosting}"
BUCKET_NAME="${BUCKET_NAME:-sentinelgrid-frontend-401429382000}"
AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"
AWS_REGION="${AWS_REGION:-ap-south-1}"

if [[ ! -d "${DIST_DIR}" ]]; then
  echo "ERROR: ${DIST_DIR} does not exist." >&2
  echo "Run 'npm run build' inside frontend/ first -- this script does not build for you." >&2
  exit 1
fi

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
  --parameter-overrides "BucketName=${BUCKET_NAME}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}"

DISTRIBUTION_ID="$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" \
  --output text)"

echo ""
echo "Syncing ${DIST_DIR}/ to s3://${BUCKET_NAME}/ ..."

# Hashed static assets first: long-lived, immutable caching. Excludes
# index.html so it is never accidentally caught by this broad rule.
aws s3 sync "${DIST_DIR}/" "s3://${BUCKET_NAME}/" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --delete \
  --exclude "index.html" \
  --cache-control "public, max-age=31536000, immutable"

# index.html last, explicitly: always-revalidate so a new deployment is
# never served stale from a browser/CDN cache.
aws s3 cp "${DIST_DIR}/index.html" "s3://${BUCKET_NAME}/index.html" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --cache-control "no-cache" \
  --content-type "text/html; charset=utf-8"

echo ""
echo "Creating CloudFront invalidation for distribution ${DISTRIBUTION_ID}..."
INVALIDATION_ID="$(aws cloudfront create-invalidation \
  --distribution-id "${DISTRIBUTION_ID}" \
  --paths "/" "/index.html" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "Invalidation.Id" \
  --output text)"
echo "Invalidation created: ${INVALIDATION_ID}"

echo ""
echo "Stack outputs:"
aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs" \
  --output table

echo ""
echo "Deployment complete. It may take a few minutes for the CloudFront"
echo "invalidation to finish propagating to all edge locations."
