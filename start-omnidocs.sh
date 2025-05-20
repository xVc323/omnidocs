#!/bin/bash

echo "Starting OmniDocs Services..."

# Check if Redis is running
echo "Checking Redis status..."
redis-cli ping &> /dev/null
if [ $? -ne 0 ]; then
  echo "Starting Redis..."
  redis-server --daemonize yes
  sleep 2
else
  echo "Redis is already running."
fi

# Load environment variables from .env
echo "Loading environment variables..."
if [ -f .env ]; then
  set -a
  source .env
  set +a
else
  echo "Warning: .env file not found, using system environment variables."
fi

# Start Celery worker
echo "Starting Celery worker..."
cd "$(dirname "$0")"  # Move to script directory
PYTHONWARNINGS="ignore::UserWarning:gevent" celery -A celery_app.celery_app worker -l info -P gevent &
CELERY_PID=$!

# Start Python API
echo "Starting Python FastAPI backend..."
uvicorn api_main:app --host 0.0.0.0 --port 8000 &
API_PID=$!

# Wait for the API to be ready
echo "Waiting for API to be ready..."
while ! curl -s http://localhost:8000 > /dev/null; do
  sleep 1
done

# Start Next.js frontend
echo "Starting Next.js frontend..."
cd frontend
npm run dev &
NEXT_PID=$!

echo "OmniDocs is running:"
echo "* Python API: http://localhost:8000"
echo "* Next.js frontend: http://localhost:3000"
echo
echo "Press Ctrl+C to stop all services"

# Setup handler to kill all processes on exit
function cleanup {
  echo
  echo "Shutting down services..."
  kill $NEXT_PID
  kill $API_PID
  kill $CELERY_PID
  echo "Done"
}

trap cleanup EXIT

# Wait for user to press Ctrl+C
wait 