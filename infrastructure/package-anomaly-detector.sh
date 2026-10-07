#!/usr/bin/env bash
# Packages the anomaly detector into a zip deployable to AWS Lambda.
#
# The handler (lambda_functions/anomaly_detector/handler.py), detector
# (detector.py), event store (event_store.py), and remediation trigger
# client (remediation_trigger_client.py, added Step 22) currently import
# nothing beyond the Python standard library and boto3 (which the Lambda
# runtime already provides) — see their own `import`/`from` lines. So
# this script does NOT run `pip install`: it only copies the source files
# the handler actually needs. If a future version gains a real
# third-party dependency not already provided by the Lambda runtime, this
# script will need a `pip install -r requirements.txt --target "$BUILD_DIR"`
# step added.
#
# IMPORTANT: every .py file lambda_functions/anomaly_detector/ needs at
# runtime must be listed explicitly below — adding a new module there
# (as event_store.py once was) without adding it here silently produces
# a Lambda package that fails with ModuleNotFoundError on every
# invocation. tests/test_anomaly_detector_infrastructure.py asserts each
# required file's presence in the built zip to catch this.
#
# `shared/` is included even though nothing in lambda_functions/ imports
# it today, per the project's package boundary — a future detector change
# (e.g. using shared.telemetry's schema directly) may need it, and it's a
# few small, dependency-free files.
#
# Excluded (deliberately): venv/, tests/, logs/, infrastructure/, .git,
# READMEs, examples/, config YAML, and anything else not required at
# runtime.
#
# Usage:
#   ./infrastructure/package-anomaly-detector.sh [output_zip_path]
#
# Defaults to writing: infrastructure/build/anomaly-detector.zip
#
# This script makes NO AWS calls and requires no credentials — it only
# reads local source files and writes a local zip.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_ROOT="${SCRIPT_DIR}/build"
STAGING_DIR="${BUILD_ROOT}/anomaly-detector-staging"
OUTPUT_ZIP="${1:-${BUILD_ROOT}/anomaly-detector.zip}"

echo "Packaging SentinelGrid anomaly detector for AWS Lambda..."

rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}/lambda_functions/anomaly_detector"
mkdir -p "${STAGING_DIR}/shared"

# lambda_functions/ — handler + detector code only (no README, no examples/)
cp "${REPO_ROOT}/lambda_functions/__init__.py" \
   "${STAGING_DIR}/lambda_functions/"
cp "${REPO_ROOT}/lambda_functions/anomaly_detector/__init__.py" \
   "${REPO_ROOT}/lambda_functions/anomaly_detector/detector.py" \
   "${REPO_ROOT}/lambda_functions/anomaly_detector/handler.py" \
   "${REPO_ROOT}/lambda_functions/anomaly_detector/event_store.py" \
   "${REPO_ROOT}/lambda_functions/anomaly_detector/remediation_trigger_client.py" \
   "${STAGING_DIR}/lambda_functions/anomaly_detector/"

# shared/ — code only (README excluded)
cp "${REPO_ROOT}"/shared/*.py "${STAGING_DIR}/shared/"

mkdir -p "$(dirname "${OUTPUT_ZIP}")"
rm -f "${OUTPUT_ZIP}"
( cd "${STAGING_DIR}" && zip -qr "${OUTPUT_ZIP}" . -x '*__pycache__*' )

echo "Package written to: ${OUTPUT_ZIP}"
echo ""
echo "Contents:"
unzip -l "${OUTPUT_ZIP}"
