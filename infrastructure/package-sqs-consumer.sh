#!/usr/bin/env bash
# Packages the SQS consumer into a zip deployable to AWS Lambda.
#
# The handler (lambda_functions/sqs_consumer/handler.py) and its
# Analyzer-invocation client (anomaly_detector_client.py) currently
# import nothing beyond the Python standard library and boto3 (which the
# Lambda runtime already provides) — see their own `import`/`from`
# lines. So this script does NOT run `pip install`: it only copies the
# source files the handler actually needs.
#
# IMPORTANT (import-graph note, verified by inspection, not assumption):
# lambda_functions/sqs_consumer/__init__.py eagerly imports BOTH
# anomaly_detector_client.py AND handler.py, so importing this package
# at all requires both files. Unlike package-remediation-trigger.sh and
# package-remediation-lambdas.sh, this package's handler.py does NOT
# import anything from lambda_functions.anomaly_detector — the Step 26
# architecture decision (see lambda_functions/sqs_consumer/README.md,
# "Why this package does NOT duplicate the Analyzer") invokes the
# existing deployed Analyzer purely by ARN, never by importing its
# source. So the Step 22 packaging-ripple-effect this project has hit
# before (a package silently missing a module the Analyzer's
# __init__.py eagerly imports) does not apply here: this package needs
# nothing from lambda_functions/anomaly_detector/ at all.
#
# `shared/` is NOT included: nothing in lambda_functions/sqs_consumer/
# imports it.
#
# Excluded (deliberately): venv/, tests/, logs/, infrastructure/, .git,
# READMEs, and anything else not required at runtime.
#
# Usage:
#   ./infrastructure/package-sqs-consumer.sh [output_zip_path]
#
# Defaults to writing: infrastructure/build/sqs-consumer.zip
#
# This script makes NO AWS calls and requires no credentials — it only
# reads local source files and writes a local zip.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_ROOT="${SCRIPT_DIR}/build"
STAGING_DIR="${BUILD_ROOT}/sqs-consumer-staging"
OUTPUT_ZIP="${1:-${BUILD_ROOT}/sqs-consumer.zip}"

echo "Packaging SentinelGrid SQS consumer for AWS Lambda..."

rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}/lambda_functions/sqs_consumer"

cp "${REPO_ROOT}/lambda_functions/__init__.py" \
   "${STAGING_DIR}/lambda_functions/"

cp "${REPO_ROOT}/lambda_functions/sqs_consumer/__init__.py" \
   "${REPO_ROOT}/lambda_functions/sqs_consumer/handler.py" \
   "${REPO_ROOT}/lambda_functions/sqs_consumer/anomaly_detector_client.py" \
   "${STAGING_DIR}/lambda_functions/sqs_consumer/"

mkdir -p "$(dirname "${OUTPUT_ZIP}")"
rm -f "${OUTPUT_ZIP}"
( cd "${STAGING_DIR}" && zip -qr "${OUTPUT_ZIP}" . -x '*__pycache__*' )

echo "Package written to: ${OUTPUT_ZIP}"
echo ""
echo "Contents:"
unzip -l "${OUTPUT_ZIP}"
