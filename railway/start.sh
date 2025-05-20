#!/bin/sh

# Default to port 8000 if PORT is not set
PORT=${PORT:-8000}

# Install supervisor if not already installed
pip install supervisor

# Create supervisor config
cat > /app/supervisord.conf << EOL
[supervisord]
nodaemon=true
user=root
logfile=/dev/stdout
logfile_maxbytes=0

[program:api]
command=uvicorn api_main:app --host 0.0.0.0 --port ${PORT}
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
autorestart=true

[program:celery]
command=celery -A celery_app.celery_app worker -l info
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
autorestart=true
EOL

# Start supervisor
supervisord -c /app/supervisord.conf 