#!/usr/bin/env bash
# Packages the knowledge-query Lambda into a zip deployable to AWS Lambda.
#
# The handler (lambda_functions/knowledge_query/handler.py), query
# service (query_service.py), and metrics service (metrics_service.py,
# Step 43) currently import nothing beyond the Python standard library
# and boto3 (which the Lambda runtime already provides) -- see their own
# `import`/`from` lines. So this script does NOT run `pip install`: it
# only copies the source files the handler actually needs.
#
# IMPORTANT: every .py file lambda_functions/knowledge_query/ needs at
# runtime must be listed explicitly below -- omitting a new module here
# after adding it to the package would silently produce a Lambda package
# that fails with ModuleNotFoundError on every invocation (see the Step
# 11/22/28/33/34 ripple-effect precedent documented in
# package-anomaly-detector.sh / package-remediation-lambdas.sh).
# tests/test_knowledge_query_infrastructure.py asserts each required
# file's presence in the built zip to catch this.
#
# This package is deliberately self-contained: it does NOT import
# lambda_functions.anomaly_detector, lambda_functions.remediation,
# remediation/, or shared/ -- the query interface has no dependency on
# the detection/remediation pipeline's own code, only on the three
# DynamoDB tables those Lambdas happen to also write to. Confirmed by
# inspection (every import/from line in handler.py and query_service.py).
#
# Excluded (deliberately): venv/, tests/, logs/, infrastructure/, .git,
# READMEs, and anything else not required at runtime.
#
# Usage:
#   ./infrastructure/package-knowledge-query.sh [output_zip_path]
#
# Defaults to writing: infrastructure/build/knowledge-query.zip
#
# This script makes NO AWS calls and requires no credentials -- it only
# reads local source files and writes a local zip.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_ROOT="${SCRIPT_DIR}/build"
STAGING_DIR="${BUILD_ROOT}/knowledge-query-staging"
OUTPUT_ZIP="${1:-${BUILD_ROOT}/knowledge-query.zip}"

echo "Packaging SentinelGrid knowledge-query Lambda for AWS Lambda..."

rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}/lambda_functions/knowledge_query"

cp "${REPO_ROOT}/lambda_functions/__init__.py" \
   "${STAGING_DIR}/lambda_functions/"

cp "${REPO_ROOT}/lambda_functions/knowledge_query/__init__.py" \
   "${REPO_ROOT}/lambda_functions/knowledge_query/handler.py" \
   "${REPO_ROOT}/lambda_functions/knowledge_query/query_service.py" \
   "${REPO_ROOT}/lambda_functions/knowledge_query/metrics_service.py" \
   "${STAGING_DIR}/lambda_functions/knowledge_query/"

mkdir -p "$(dirname "${OUTPUT_ZIP}")"
rm -f "${OUTPUT_ZIP}"
( cd "${STAGING_DIR}" && zip -qr "${OUTPUT_ZIP}" . -x '*__pycache__*' )

echo "Package written to: ${OUTPUT_ZIP}"
echo ""
echo "Contents:"
unzip -l "${OUTPUT_ZIP}"
