import eventlet
eventlet.hubs.use_hub('poll') # Or 'selects' or other available hub
eventlet.monkey_patch(all=True, thread=False) # Explicitly patch, try without thread if issues persist

import asyncio
import json
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional
import logging

from celery_app import celery_app
from tasks import process_site_task
from celery.result import AsyncResult

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Silence gevent threading warnings
logging.getLogger("gevent.threading").setLevel(logging.ERROR)

app = FastAPI(
    title="OmniDocs API",
    description="API for converting online documentation to Markdown and ZIP.",
    version="0.2.0"
)

class ConversionRequest(BaseModel):
    url: str
    paths_to_include: Optional[List[str]] = None # If empty or None, uses default scoping
    paths_to_exclude: Optional[List[str]] = None # Regex patterns
    max_pages: Optional[int] = 50 # Default max pages to crawl
    output_format: Optional[str] = "zip" # Add output_format, default to zip

@app.post("/convert", status_code=202)
async def start_conversion(request: ConversionRequest, background_tasks: BackgroundTasks):
    """
    Accepts a URL and optional include/exclude paths/regexes,
    and starts a background task for documentation conversion.
    Returns a task ID.
    """
    logger.info(f"Received conversion request for URL: {request.url} with output_format: {request.output_format}")
    
    # Prepare arguments for the Celery task
    # The Celery task process_site_task expects:
    # site_url: str, path_prefix: Optional[str] = None, use_regex: bool = False, custom_regex: Optional[str] = None, max_pages: int = 50

    # Adapt ConversionRequest to process_site_task parameters
    path_prefix_str = ",".join(request.paths_to_include) if request.paths_to_include else None
    
    use_regex_bool = bool(request.paths_to_exclude)
    custom_regex_str = "\n".join(request.paths_to_exclude) if request.paths_to_exclude else None

    try:
        task = process_site_task.delay(
            site_url=request.url,
            path_prefix=path_prefix_str,
            use_regex=use_regex_bool,
            custom_regex=custom_regex_str,
            max_pages=request.max_pages,
            output_format=request.output_format # Pass output_format to the Celery task
        )
        logger.info(f"Task {task.id} for URL {request.url} dispatched to Celery.")
        return {"task_id": task.id, "status_url": f"/stream/{task.id}"}
    except Exception as e:
        logger.error(f"Failed to dispatch Celery task for URL {request.url}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error dispatching task: {str(e)}")


async def progress_streamer(task_id: str):
    """
    Streams progress updates for a given task_id.
    """
    logger.info(f"Starting progress stream for task_id: {task_id}")
    last_meta_sent = None
    try:
        while True:
            task_result = AsyncResult(task_id, app=celery_app)
            current_meta = {}

            if task_result.state == 'PENDING':
                current_meta = {'status': 'PENDING', 'message': 'Task is waiting to be processed.'}
            elif task_result.state == 'STARTED':
                current_meta = {'status': 'STARTED', 'message': 'Task has started.'}
                if task_result.info and isinstance(task_result.info, dict):
                    current_meta.update(task_result.info)
            elif task_result.state == 'PROGRESS':
                if task_result.info and isinstance(task_result.info, dict):
                    current_meta = task_result.info # task_result.info should contain all progress data
                else:
                    current_meta = {'status': 'PROGRESS', 'message': 'Task is in progress.', 'details': str(task_result.info)}
            elif task_result.state in ['SUCCESS', 'FAILURE', 'REVOKED']:
                logger.info(f"Task {task_id} finished with state: {task_result.state}")
                if task_result.state == 'SUCCESS':
                    current_meta = {'status': 'SUCCESS', 'message': 'Task completed successfully.', 'result': task_result.result}
                elif task_result.state == 'FAILURE':
                    current_meta = {'status': 'FAILURE', 'message': 'Task failed.', 'error': str(task_result.info)}
                else: # REVOKED or other terminal states
                    current_meta = {'status': task_result.state, 'message': f'Task is in state: {task_result.state}'}
                
                if current_meta != last_meta_sent: # Send final state
                    yield f"data: {json.dumps(current_meta)}\n\n"
                    last_meta_sent = current_meta
                break # Exit loop for terminal states
            else: # Other states
                current_meta = {'status': task_result.state, 'message': f'Task is in an unknown state: {task_result.state}'}


            if current_meta != last_meta_sent:
                yield f"data: {json.dumps(current_meta)}\n\n"
                last_meta_sent = current_meta
            
            await asyncio.sleep(1) # Poll interval
            
    except Exception as e:
        logger.error(f"Error in progress streamer for task {task_id}: {e}", exc_info=True)
        error_event = {"status": "STREAM_ERROR", "message": "An error occurred while streaming progress.", "detail": str(e)}
        try:
            yield f"data: {json.dumps(error_event)}\n\n"
        except Exception as e_yield: # Handle if yield itself fails (e.g., client disconnected)
            logger.warning(f"Failed to yield error event to client for task {task_id}: {e_yield}")
    finally:
        logger.info(f"Progress stream for task {task_id} ended.")


@app.get("/stream/{task_id}")
async def stream_task_progress(task_id: str, request: Request):
    """
    Endpoint to stream progress updates for a given Celery task ID.
    Uses Server-Sent Events (SSE).
    """
    logger.info(f"Client connected for task_id stream: {task_id}")
    return StreamingResponse(progress_streamer(task_id), media_type="text/event-stream")

# Optional: A root endpoint for basic API health check
@app.get("/")
async def root():
    return {"message": "OmniDocs API is running. Use /convert to start a job and /stream/{task_id} to monitor."}

if __name__ == "__main__":
    # This block is for local development with Uvicorn if you run `python main.py`
    # For production, you'd typically use `uvicorn main:app --host 0.0.0.0 --port 80`
    import uvicorn
    logger.info("Starting Uvicorn server for local development...")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info") 