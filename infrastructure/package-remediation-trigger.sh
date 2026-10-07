#!/usr/bin/env bash
# Packages the remediation trigger Lambda into a zip deployable to AWS
# Lambda.
#
# IMPORTANT (import-graph note, verified by inspection, not assumption):
# lambda_functions/remediation_trigger/__init__.py eagerly imports BOTH
# trigger.py AND handler.py; Python always executes a package's
# __init__.py before any of its submodules, so importing this package at
# all requires both files. handler.py in turn imports
# lambda_functions.anomaly_detector.detector, which forces
# lambda_functions/anomaly_detector/__init__.py to run first -- and THAT
# eagerly imports detector.py, event_store.py, AND handler.py (the
# anomaly detector's own handler, unrelated to this package's handler.py).
# This mirrors exactly the same situation already solved for
# package-remediation-lambdas.sh -- see that script's own header comment
# for the full reasoning, and infrastructure/README.md's account of the
# Step 11 packaging bug (event_store.py once missing from the anomaly
# detector's own package despite being imported) this careful,
# explicit-file-list approach exists to avoid repeating.
#
# event_store.py imports boto3/botocore, and this package's own
# handler.py does too. Neither is bundled here -- the AWS Lambda Python
# runtime provides boto3/botocore natively, exactly like
# package-anomaly-detector.sh and package-remediation-lambdas.sh already
# rely on.
#
# lambda_functions/anomaly_detector/detector.py was already confirmed
# (in Step 17) to need nothing from shared/.
#
# (Step 28) lambda_functions/anomaly_detector/handler.py now imports
# shared/metrics.py (CloudWatch custom metrics) -- this package bundles
# that same handler.py (see the __init__.py chain above), so shared/
# metrics.py IS now required here, confirmed the hard way via a failed
# isolated-import test (ModuleNotFoundError: No module named 'shared')
# before this fix. (Step 34: shared/node_state_store.py is required for
# the exact same reason.) Only these two files are copied from shared/,
# not the whole directory -- nothing else in shared/ is imported by
# anything in this package.
#
# This script does NOT run `pip install`: nothing here needs a
# third-party package beyond boto3/botocore, which the Lambda runtime
# already provides.
#
# Excluded (deliberately): venv/, tests/, logs/, infrastructure/, .git,
# READMEs, and anything else not required at runtime.
#
# Usage:
#   ./infrastructure/package-remediation-trigger.sh [output_zip_path]
#
# Defaults to writing: infrastructure/build/remediation-trigger.zip
#
# This script makes NO AWS calls and requires no credentials — it only
# reads local source files and writes a local zip.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_ROOT="${SCRIPT_DIR}/build"
STAGING_DIR="${BUILD_ROOT}/remediation-trigger-staging"
OUTPUT_ZIP="${1:-${BUILD_ROOT}/remediation-trigger.zip}"

echo "Packaging SentinelGrid remediation trigger for AWS Lambda..."

rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}/lambda_functions/anomaly_detector"
mkdir -p "${STAGING_DIR}/lambda_functions/remediation_trigger"
mkdir -p "${STAGING_DIR}/shared"

cp "${REPO_ROOT}/lambda_functions/__init__.py" \
   "${STAGING_DIR}/lambda_functions/"

cp "${REPO_ROOT}/shared/__init__.py" \
   "${REPO_ROOT}/shared/metrics.py" \
   "${REPO_ROOT}/shared/node_state_store.py" \
   "${STAGING_DIR}/shared/"

cp "${REPO_ROOT}/lambda_functions/anomaly_detector/__init__.py" \
   "${REPO_ROOT}/lambda_functions/anomaly_detector/detector.py" \
   "${REPO_ROOT}/lambda_functions/anomaly_detector/event_store.py" \
   "${REPO_ROOT}/lambda_functions/anomaly_detector/handler.py" \
   "${REPO_ROOT}/lambda_functions/anomaly_detector/remediation_trigger_client.py" \
   "${STAGING_DIR}/lambda_functions/anomaly_detector/"

cp "${REPO_ROOT}/lambda_functions/remediation_trigger/__init__.py" \
   "${REPO_ROOT}/lambda_functions/remediation_trigger/trigger.py" \
   "${REPO_ROOT}/lambda_functions/remediation_trigger/handler.py" \
   "${STAGING_DIR}/lambda_functions/remediation_trigger/"

mkdir -p "$(dirname "${OUTPUT_ZIP}")"
rm -f "${OUTPUT_ZIP}"
( cd "${STAGING_DIR}" && zip -qr "${OUTPUT_ZIP}" . -x '*__pycache__*' )

echo "Package written to: ${OUTPUT_ZIP}"
echo ""
echo "Contents:"
unzip -l "${OUTPUT_ZIP}"
