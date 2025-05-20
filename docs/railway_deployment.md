# Railway Deployment Guide for OmniDocs

This guide explains how to deploy OmniDocs as a multi-service application on Railway.

## Prerequisites

- Railway account
- Railway CLI installed (`npm i -g @railway/cli`)
- Git repository with OmniDocs code

## Simplified Deployment Process

Due to changes in the Railway CLI, we've simplified the deployment process. The current recommended approach is:

1. **Deploy backend and frontend as separate services in the same project**
2. **Configure environment variables and add Redis plugin manually**
3. **Set up custom domain for the frontend service**

## Step 1: Backend Deployment

First, deploy the backend service:

```bash
# Login to Railway
railway login --browserless

# Deploy backend service
cp railway/backend.toml railway.toml
railway up
```

After deployment, go to the Railway dashboard and:

1. Add the Redis plugin to your project
2. Configure R2 environment variables:
   - `R2_ACCOUNT_ID`
   - `R2_ACCESS_KEY_ID`
   - `R2_SECRET_ACCESS_KEY`
   - `R2_BUCKET_NAME`

## Step 2: Frontend Deployment

Now deploy the frontend service:

```bash
# Deploy frontend
cp railway/frontend.toml railway.toml
railway up
```

After deployment, go to the Railway dashboard and:

1. Update the `NEXT_PUBLIC_API_URL` environment variable with the actual URL of your backend service
2. Set up your custom domain for the frontend service

## Step 3: Custom Domain Setup

To use a custom domain (e.g., omnidocs.pat.network):

1. Go to Railway dashboard → Your Project → Settings → Domains
2. Click "Add Domain"
3. Enter your domain (e.g., omnidocs.pat.network)
4. Configure DNS with your provider (use the CNAME record provided by Railway)
5. Wait for DNS propagation and SSL certificate generation

## Troubleshooting

### Deployment Fails with Environment Variable Issues

If deployment fails with `$PORT` errors:
- Make sure to use the included `start.sh` script which handles the PORT variable correctly
- Set explicit port values in the Railway configuration files

### ESLint Errors During Frontend Build

If you encounter ESLint errors during the frontend build:
- The Dockerfile has been updated to skip ESLint with `ENV ESLINT_SKIP true`
- An `.eslintignore` file has been added to ignore problematic files

### Services Can't Connect to Each Other

If the frontend can't connect to the backend:
1. Get the backend URL from the Railway dashboard
2. Set it as the `NEXT_PUBLIC_API_URL` environment variable in the frontend service settings

### Redis Connection Issues

If you encounter Redis connection issues:
1. Make sure the Redis plugin is added to your project
2. Check that the `REDIS_URL` environment variable is being passed to your backend service
3. You may need to restart the backend service after adding Redis

## Reference Commands

Here are some useful Railway CLI commands for managing your deployment:

```bash
# Get current project status
railway status

# View logs
railway logs

# Open project in dashboard
railway open

# Add Redis plugin
railway add

# List available variables
railway variables

# Set a variable
railway variables set KEY=VALUE
```

Remember that the Railway CLI is actively developed and commands may change. When in doubt, use `railway help` to see the current available commands. 