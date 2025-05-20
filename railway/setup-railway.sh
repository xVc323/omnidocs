#!/bin/bash
# Script to set up multi-service deployment on Railway

# Colors for terminal output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Setting up OmniDocs multi-service deployment on Railway${NC}"

# Check if Railway CLI is installed
if ! command -v railway &> /dev/null; then
    echo -e "${YELLOW}Railway CLI not found. Installing...${NC}"
    npm i -g @railway/cli
    if [ $? -ne 0 ]; then
        echo "Failed to install Railway CLI. Please install it manually with 'npm i -g @railway/cli'."
        exit 1
    fi
fi

# Ensure the user is logged in using browserless method
echo -e "${GREEN}Logging in to Railway (browserless method)...${NC}"
railway login --browserless

# Deploy backend
echo -e "${GREEN}Deploying backend service...${NC}"
cp railway/backend.toml railway.toml
railway up

# Add Redis (you'll need to do this manually in the dashboard)
echo -e "${YELLOW}Please add Redis plugin manually through the Railway dashboard.${NC}"

# Deploy frontend
echo -e "${GREEN}Deploying frontend service...${NC}"
cp railway/frontend.toml railway.toml
railway up

echo -e "${GREEN}Deployment initiated!${NC}"
echo "Please check the Railway dashboard for deployment status and URLs."
echo "You'll need to manually:"
echo "  1. Add the Redis plugin to the backend service"
echo "  2. Configure environment variables for R2 storage"
echo "  3. Set up your custom domain (omnidocs.pat.network)" 