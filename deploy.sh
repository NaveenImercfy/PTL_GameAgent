#!/bin/bash
# =============================================================================
# Deploy Home Agent to Google Cloud Run
# =============================================================================
# Prerequisites:
#   1. Install Google Cloud SDK: https://cloud.google.com/sdk/docs/install
#   2. Login: gcloud auth login
#   3. Set project: gcloud config set project YOUR_PROJECT_ID
#   4. Enable APIs:
#      gcloud services enable run.googleapis.com
#      gcloud services enable cloudbuild.googleapis.com
#      gcloud services enable artifactregistry.googleapis.com
# =============================================================================

set -e

# ---- CONFIGURATION (edit these) ----
PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-your-project-id}"
REGION="${CLOUD_RUN_REGION:-us-central1}"
SERVICE_NAME="home-agent-service"

# Environment variables for the agent
GOOGLE_API_KEY="${GOOGLE_API_KEY:-AIzaSyDyQGkwBuBccdYvcOLoFHsixxC-b35oxg0}"
QUESTION_API_STD="${QUESTION_API_STD:-8}"
QUESTIONS_SOURCE_API_URL="${QUESTIONS_SOURCE_API_URL:-https://question-751927247815.us-east4.run.app/}"
QUESTIONS_SOURCE_API_METHOD="${QUESTIONS_SOURCE_API_METHOD:-GET}"

echo "============================================="
echo "Deploying Home Agent to Cloud Run"
echo "  Project:  $PROJECT_ID"
echo "  Region:   $REGION"
echo "  Service:  $SERVICE_NAME"
echo "============================================="

# Deploy to Cloud Run (builds container automatically)
gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --port 8080 \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --allow-unauthenticated \
  --set-env-vars="GOOGLE_API_KEY=$GOOGLE_API_KEY,QUESTION_API_STD=$QUESTION_API_STD,QUESTIONS_SOURCE_API_URL=$QUESTIONS_SOURCE_API_URL,QUESTIONS_SOURCE_API_METHOD=$QUESTIONS_SOURCE_API_METHOD,USE_FIRESTORE=true" \
  --memory 1Gi \
  --cpu 1 \
  --timeout 60 \
  --concurrency 80 \
  --min-instances 1 \
  --max-instances 5

echo ""
echo "Deployment complete!"
echo "Service URL:"
gcloud run services describe "$SERVICE_NAME" \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --format "value(status.url)"
