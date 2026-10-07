#!/usr/bin/env bash
# Deploys SentinelGrid's CloudWatch alarm + SNS escalation stack:
#   AWS::SNS::Topic (SentinelGrid-UnresolvedFaults)
#   AWS::CloudWatch::Alarm (SentinelGrid-UnresolvedFaults-Alarm)
#   (infrastructure/observability-alerting.yaml)
#
# This script reads NO credentials and embeds NONE. It relies entirely on
# the AWS CLI's normal profile/credential resolution.
#
# This template creates no IAM resources (CloudWatch Alarm -> SNS is
# same-account service-to-service behavior, no topic policy needed), no
# new metrics, no Lambda/DynamoDB/SQS/Step Functions resources, and no
# SNS subscription. No --capabilities flag is needed. This stack does
# not modify any other stack, any Lambda, any IoT rule, the SQS queue,
# the Step Functions state machine, or the existing dashboard.
#
# Usage:
#   ./infrastructure/deploy-observability-alerting.sh
#
# Override the profile/region/threshold/period if needed:
#   AWS_PROFILE=my-profile AWS_REGION=us-east-1 \
#   ALARM_THRESHOLD=1 ALARM_PERIOD_SECONDS=300 \
#     ./infrastructure/deploy-observability-alerting.sh
#
# This script is NOT invoked automatically by anything in this repo — run
# it yourself, deliberately, when you're ready to deploy. It has not been
# executed as part of writing it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/observability-alerting.yaml"

STACK_NAME="${STACK_NAME:-sentinelgrid-observability-alerting}"
AWS_PROFILE="${AWS_PROFILE:-sentinelgrid}"
AWS_REGION="${AWS_REGION:-ap-south-1}"
ALARM_THRESHOLD="${ALARM_THRESHOLD:-1}"
ALARM_PERIOD_SECONDS="${ALARM_PERIOD_SECONDS:-300}"

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
  --parameter-overrides \
      "AlarmThreshold=${ALARM_THRESHOLD}" \
      "AlarmPeriodSeconds=${ALARM_PERIOD_SECONDS}" \
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
echo "NOTE: no SNS subscription was created. To receive real"
echo "notifications, subscribe a verified endpoint manually, e.g.:"
echo "  aws sns subscribe --topic-arn <TopicArn> --protocol email \\"
echo "    --notification-endpoint <your-verified-email> \\"
echo "    --profile ${AWS_PROFILE} --region ${AWS_REGION}"
echo "and confirm the subscription from the email AWS sends."
