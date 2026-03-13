#!/bin/bash
# =============================================================================
# Deploy Home Agent to Vertex AI Agent Engine
# =============================================================================
# Prerequisites:
#   1. Install Google Cloud SDK
#   2. Login: gcloud auth login && gcloud auth application-default login
#   3. Enable APIs:
#      gcloud services enable aiplatform.googleapis.com
#      gcloud services enable storage.googleapis.com
#   4. Install ADK:
#      pip install google-cloud-aiplatform[agent_engines,adk]>=1.112
# =============================================================================

set -e

# Fix encoding for Windows (ADK CLI prints Unicode chars)
export PYTHONIOENCODING=utf-8

# ---- CONFIGURATION ----
PROJECT_ID="aitrack-29a9e"
REGION="us-east4"
DISPLAY_NAME="Home Assistant AI"
AGENT_DIR="./Home_Agent"

echo "============================================="
echo "Deploying Home Agent to Vertex AI Agent Engine"
echo "  Project:  $PROJECT_ID"
echo "  Region:   $REGION"
echo "============================================="

# Deploy using ADK CLI (staging_bucket is deprecated/not needed)
adk deploy agent_engine \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --display_name="$DISPLAY_NAME" \
  --trace_to_cloud \
  "$AGENT_DIR"

echo ""
echo "Deployment complete! Check the console:"
echo "https://console.cloud.google.com/vertex-ai/agents/agent-engines?project=${PROJECT_ID}&vertex_ai_region=${REGION}"
