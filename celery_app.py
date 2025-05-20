from celery import Celery
import logging
from datetime import timedelta
import os

# Filter out gevent thread warning logs
logging.getLogger("gevent.threading").setLevel(logging.ERROR)

# Use environment variable if available, otherwise fallback to localhost
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
print(f"Using Redis URL: {REDIS_URL}")

celery_app = Celery(
    "tasks",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["tasks"]  # Name of the module where tasks will be defined
)

# Configure the Celery application
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    worker_concurrency=1, # Adjust based on your workload and resources (CPU-bound vs I/O bound)
    worker_prefetch_multiplier=1, # Can help with long-running tasks to prevent workers from hoarding tasks
    
    # Enable beat scheduler for periodic tasks
    beat_schedule={
        'cleanup-expired-r2-objects-every-hour': {
            'task': 'tasks.cleanup_expired_r2_objects',
            'schedule': timedelta(hours=1),
        },
    },
)

if __name__ == "__main__":
    celery_app.start() 