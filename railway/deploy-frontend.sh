#!/bin/bash
# OmniDocs Frontend Railway deployment script

# Colors for terminal output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=======================================${NC}"
echo -e "${GREEN}   Deploying OmniDocs Frontend to Railway      ${NC}"
echo -e "${GREEN}=======================================${NC}"

# Check if Railway CLI is installed
if ! command -v railway &> /dev/null; then
    echo -e "${YELLOW}Railway CLI not found. Installing...${NC}"
    npm i -g @railway/cli
    if [ $? -ne 0 ]; then
        echo "Failed to install Railway CLI. Please install it manually with 'npm i -g @railway/cli'."
        exit 1
    fi
fi

# Ensure the user is logged in
if ! railway whoami &> /dev/null; then
    echo -e "${GREEN}Logging in to Railway...${NC}"
    railway login --browserless
fi

echo -e "${GREEN}Using existing project omnidocs-frontend...${NC}"
railway status

echo -e "${GREEN}Setting environment variables...${NC}"
railway variables --set "NEXT_PUBLIC_API_URL=https://omnidocs-backend-production.up.railway.app"

echo -e "${GREEN}Deploying frontend service...${NC}"
cp railway/frontend.toml railway.toml
railway up

echo -e "${GREEN}Deployment initiated.${NC}"
echo "Check the Railway dashboard for deployment status."
echo -e "${YELLOW}Frontend URL: https://omnidocs-frontend-production.up.railway.app${NC}" 