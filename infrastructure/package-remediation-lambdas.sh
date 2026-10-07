#!/usr/bin/env bash
# Packages the three remediation Lambda adapters into three zips deployable
# to AWS Lambda: remediation-planner.zip, remediation-executor.zip,
# remediation-verifier.zip.
#
# IMPORTANT (import-graph note, verified by inspection, not assumption):
# lambda_functions/remediation/__init__.py eagerly imports all THREE
# handler modules (planner, executor, verifier); Python always executes a
# package's __init__.py before any of its submodules, so importing ANY ONE
# of lambda_functions.remediation.{planner,executor,verifier} transitively
# requires all three .py files to be present, even though each individual
# Lambda only calls one of them. Likewise, planner.py imports
# lambda_functions.anomaly_detector.detector, which forces
# lambda_functions/anomaly_detector/__init__.py to run first -- and THAT
# eagerly imports detector.py, event_store.py, AND handler.py. And
# remediation/__init__.py eagerly imports engine.py, integration.py, AND
# models.py. All three packages below therefore contain the IDENTICAL file
# set -- this is deliberate, not accidental bloat, and exists specifically
# to avoid repeating the Step 11 packaging bug (event_store.py once
# missing from the anomaly detector's own package despite being imported).
#
# event_store.py imports boto3/botocore. Exactly like the anomaly
# detector's own package (see package-anomaly-detector.sh), these are NOT
# bundled here -- the AWS Lambda Python runtime provides boto3/botocore
# natively. None of these three remediation Lambdas ever calls anything in
# event_store.py; it is only present because of the __init__.py chain
# above, and every isolated-import test for these packages therefore also
# relies on the runtime (or, locally, this project's own venv) providing
# boto3 -- not on it being bundled in the zip.
#
# lambda_functions/anomaly_detector/detector.py, remediation/models.py,
# remediation/engine.py, and remediation/integration.py were all inspected
# directly (every import/from line, via grep) and confirmed to need
# nothing from shared/.
#
# (Step 28) lambda_functions/anomaly_detector/handler.py now imports
# shared/metrics.py (CloudWatch custom metrics), and
# lambda_functions/remediation/{planner,executor,verifier}.py do too, so
# shared/metrics.py IS now required here -- confirmed the hard way: the
# first Step 28 packaging attempt omitted it and every isolated-import
# test failed with ModuleNotFoundError: No module named 'shared', exactly
# the Step 11/22 ripple-effect this header already warns about. Only
# metrics.py and node_state_store.py (Step 34 -- same ripple, handler.py
# now also imports shared/node_state_store.py) are copied from shared/
# (not the whole directory) -- nothing else in shared/ is imported by
# anything in these three packages.
#
# (Step 33) planner.py and executor.py now also import
# lambda_functions/remediation/recovery_event_store.py (the recovery-
# history persistence module, mirroring event_store.py's own role) --
# and because remediation/__init__.py eagerly imports all three handler
# modules, this file is required in ALL THREE packages below, exactly
# like event_store.py already is, even though verifier.py itself never
# calls it directly. recovery_event_store.py imports boto3/botocore --
# not bundled, for the same reason event_store.py's own boto3 import
# isn't: the Lambda runtime provides it natively.
#
# This script does NOT run `pip install`: nothing above needs a
# third-party package beyond boto3/botocore, which the Lambda runtime
# already provides.
#
# Excluded (deliberately): venv/, tests/, logs/, infrastructure/, .git,
# READMEs, and anything else not required at runtime.
#
# Usage:
#   ./infrastructure/package-remediation-lambdas.sh [output_dir]
#
# Defaults to writing into infrastructure/build/.
#
# This script makes NO AWS calls and requires no credentials — it only
# reads local source files and writes local zips.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_ROOT="${SCRIPT_DIR}/build"
STAGING_DIR="${BUILD_ROOT}/remediation-lambdas-staging"
OUTPUT_DIR="${1:-${BUILD_ROOT}}"

echo "Packaging SentinelGrid remediation Lambda adapters for AWS Lambda..."

rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}/lambda_functions/anomaly_detector"
mkdir -p "${STAGING_DIR}/lambda_functions/remediation"
mkdir -p "${STAGING_DIR}/remediation"
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

cp "${REPO_ROOT}/lambda_functions/remediation/__init__.py" \
   "${REPO_ROOT}/lambda_functions/remediation/planner.py" \
   "${REPO_ROOT}/lambda_functions/remediation/executor.py" \
   "${REPO_ROOT}/lambda_functions/remediation/verifier.py" \
   "${REPO_ROOT}/lambda_functions/remediation/recovery_event_store.py" \
   "${STAGING_DIR}/lambda_functions/remediation/"

cp "${REPO_ROOT}/remediation/__init__.py" \
   "${REPO_ROOT}/remediation/models.py" \
   "${REPO_ROOT}/remediation/engine.py" \
   "${REPO_ROOT}/remediation/integration.py" \
   "${STAGING_DIR}/remediation/"

mkdir -p "${OUTPUT_DIR}"

for name in planner executor verifier; do
  output_zip="${OUTPUT_DIR}/remediation-${name}.zip"
  rm -f "${output_zip}"
  ( cd "${STAGING_DIR}" && zip -qr "${output_zip}" . -x '*__pycache__*' )
  echo ""
  echo "Package written to: ${output_zip}"
  echo "Contents:"
  unzip -l "${output_zip}"
done
