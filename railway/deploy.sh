#!/bin/bash
# OmniDocs Railway deployment script

# Colors for terminal output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=======================================${NC}"
echo -e "${GREEN}   Deploying OmniDocs to Railway      ${NC}"
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

# Check which service to deploy
if [ "$1" == "backend" ]; then
    echo -e "${GREEN}Deploying backend service...${NC}"
    cp railway/backend.toml railway.toml
    railway up
    echo -e "${YELLOW}Don't forget to add Redis plugin in the Railway dashboard!${NC}"
elif [ "$1" == "frontend" ]; then
    echo -e "${GREEN}Deploying frontend service...${NC}"
    cp railway/frontend.toml railway.toml
    railway up
    echo -e "${YELLOW}Don't forget to set NEXT_PUBLIC_API_URL to point to your backend service!${NC}"
else
    echo -e "${YELLOW}Usage: ./railway/deploy.sh [backend|frontend]${NC}"
    echo "Examples:"
    echo "  ./railway/deploy.sh backend    # Deploy the backend service"
    echo "  ./railway/deploy.sh frontend   # Deploy the frontend service"
    exit 1
fi

echo -e "${GREEN}Deployment initiated.${NC}"
echo "Check the Railway dashboard for deployment status." 