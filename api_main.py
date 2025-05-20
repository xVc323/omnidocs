import asyncio
import os
import json
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import StreamingResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware  # Import CORS middleware
from pydantic import BaseModel
from celery.result import AsyncResult
from typing import Optional, Dict, Any, List
import boto3
from botocore.exceptions import NoCredentialsError, PartialCredentialsError, ClientError
from io import BytesIO

from tasks import process_site_task # Our Celery task
from celery_app import celery_app # Our Celery app instance

# Ensure the output directory exists
# This should align with where your Celery tasks save files and what your crawler.py expects.
# For now, using a relative path. Consider making this configurable.
OUTPUTS_DIR = "outputs"
os.makedirs(OUTPUTS_DIR, exist_ok=True)

app = FastAPI(
    title="OmniDocs API",
    description="API for crawling websites and converting HTML to Markdown.",
    version="0.1.0"
)

# Get CORS origins from environment variable or use default
frontend_url = os.environ.get("FRONTEND_URL", "https://omnidocs-frontend-production.up.railway.app")
cors_origins_str = os.environ.get("CORS_ALLOW_ORIGINS", frontend_url)
# Split by comma if it's a list of origins
cors_origins: List[str] = [origin.strip() for origin in cors_origins_str.split(",")] if "," in cors_origins_str else [cors_origins_str]

print(f"Configuring CORS with allowed origins: {cors_origins}")

# Add CORS middleware to allow cross-origin requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,  # Use origins from environment variable
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods
    allow_headers=["*"],  # Allow all headers
)

class ConversionRequest(BaseModel):
    site_url: str
    output_format: Optional[str] = "zip"
    path_prefix: Optional[str] = None
    use_regex: Optional[bool] = False
    custom_regex: Optional[str] = None

@app.post("/api/convert", status_code=202)
async def start_conversion(payload: ConversionRequest):
    """
    Starts a new documentation conversion job.
    Returns a job ID that can be used to track progress and retrieve results.
    """
    task = process_site_task.delay(
        site_url=payload.site_url,
        output_format=payload.output_format,
        path_prefix=payload.path_prefix,
        use_regex=payload.use_regex,
        custom_regex=payload.custom_regex
    )
    return {"job_id": task.id}

async def sse_progress_generator(job_id: str):
    """Generates Server-Sent Events for job progress."""
    result = AsyncResult(job_id, app=celery_app)
    previous_meta = None

    while not result.ready():
        if result.state == 'PROGRESS':
            meta = result.info
            if meta != previous_meta: # Send update only if meta changed
                data_payload = {"state": result.state, "meta": meta}
                yield f"data: {json.dumps(data_payload)}\n\n"
                previous_meta = meta
        elif result.state not in ['PENDING', 'STARTED']:
            # Handle failure states or other unexpected states
            data_payload = {"state": result.state, "status": "Job failed or in unexpected state."}
            yield f"data: {json.dumps(data_payload)}\n\n"
            return # Stop sending events
        await asyncio.sleep(1)  # Poll every 1 second

    # Final status update
    if result.successful():
        final_info = result.get()
        data_payload = {"state": result.state, "meta": final_info}
        yield f"data: {json.dumps(data_payload)}\n\n"
    else: # Handle task failure
        try:
            # Attempt to get traceback if available
            error_info = str(result.info)
            traceback_info = result.traceback if hasattr(result, 'traceback') else "No traceback available."
            data_payload = {"state": "FAILURE", "meta": {"error": error_info, "traceback": traceback_info}}
            yield f"data: {json.dumps(data_payload)}\n\n"
        except Exception as e:
            data_payload = {"state": "FAILURE", "meta": {"error": f"Failed to retrieve failure details: {str(e)}"}}
            yield f"data: {json.dumps(data_payload)}\n\n"

@app.get("/api/job/{job_id}/progress")
async def job_progress_sse(job_id: str):
    """
    Streams progress updates for a given job ID using Server-Sent Events.
    """
    return StreamingResponse(sse_progress_generator(job_id), media_type="text/event-stream")

@app.get("/api/job/{job_id}/status")
async def get_job_status(job_id: str):
    """
    Retrieves the current status and result (if available) of a job.
    """
    result = AsyncResult(job_id, app=celery_app)
    response_data: Dict[str, Any] = {
        "job_id": job_id,
        "state": result.state,
        "info": result.info # This could be progress metadata or the final result
    }
    if result.ready():
        if result.successful():
            response_data["result"] = result.get()
        else:
            response_data["error"] = str(result.info) # Celery stores exception info here
            if hasattr(result, 'traceback'):
                response_data["traceback"] = result.traceback
    return response_data

def get_r2_client():
    """Initializes and returns an S3 client configured for R2."""
    try:
        r2_account_id = os.environ['R2_ACCOUNT_ID']
        r2_access_key_id = os.environ['R2_ACCESS_KEY_ID']
        r2_secret_access_key = os.environ['R2_SECRET_ACCESS_KEY']
    except KeyError as e:
        raise HTTPException(status_code=500, detail=f"Configuration error: Missing R2 env var: {e}")

    endpoint_url = f'https://{r2_account_id}.r2.cloudflarestorage.com'
    try:
        s3_client = boto3.client(
            service_name='s3',
            endpoint_url=endpoint_url,
            aws_access_key_id=r2_access_key_id,
            aws_secret_access_key=r2_secret_access_key,
            region_name='auto'
        )
        return s3_client
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"R2 client initialization failed: {e}")

@app.get("/api/download/{job_id}")
async def download_output(job_id: str):
    """
    Downloads the output file for a completed job from R2 storage.
    """
    result = AsyncResult(job_id, app=celery_app)
    if not result.ready():
        raise HTTPException(status_code=404, detail="Job not completed or does not exist.")
    if not result.successful():
        raise HTTPException(status_code=500, detail=f"Job failed: {str(result.info)}")

    task_result = result.get()
    if not isinstance(task_result, dict):
        raise HTTPException(status_code=500, detail="Job result is not in the expected format.")
        
    # Check if we have R2 bucket and object information
    r2_bucket = task_result.get("r2Bucket")
    r2_object_key = task_result.get("r2ObjectKey")
    output_format = task_result.get("outputFormat", "zip")
    
    if not r2_bucket or not r2_object_key:
        raise HTTPException(status_code=500, detail="Job result does not contain R2 storage information.")

    # Initialize R2 client
    s3_client = get_r2_client()
    
    try:
        # Get object from R2
        response = s3_client.get_object(Bucket=r2_bucket, Key=r2_object_key)
        file_content = response['Body'].read()
        
        # Determine content type and filename
        content_type = response.get('ContentType', 'application/octet-stream')
        filename = os.path.basename(r2_object_key)
        
        # If ContentDisposition is present, try to extract the filename
        content_disposition = response.get('ContentDisposition', '')
        if content_disposition:
            import re
            match = re.search(r'filename="([^"]+)"', content_disposition)
            if match:
                filename = match.group(1)
        
        # If no filename found, create a generic one based on the job ID
        if not filename:
            extension = ".md" if output_format == "single_md" else ".zip"
            filename = f"omnidocs_export_{job_id}{extension}"
            
        return Response(
            content=file_content,
            media_type=content_type,
            headers={
                "Content-Disposition": f"attachment; filename=\"{filename}\"",
                "Content-Length": str(len(file_content))
            }
        )
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        if error_code == 'NoSuchKey':
            raise HTTPException(status_code=404, detail=f"File {r2_object_key} not found in R2 bucket.")
        else:
            raise HTTPException(status_code=500, detail=f"R2 error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error downloading file: {str(e)}")

@app.get("/")
async def root():
    """
    Root endpoint for Railway health checks.
    """
    return {"message": "OmniDocs API is running. Use /api/convert to start a job."}

# To run this app (from your terminal):
# 1. Start Redis: redis-server
# 2. Start Celery worker: celery -A celery_app.celery_app worker -l info -P gevent (or -P solo for testing)
#    (Ensure celery_app.py and tasks.py are in your PYTHONPATH or current directory)
# 3. Start FastAPI app: uvicorn api_main:app --reload

if __name__ == "__main__":
    # This is for development only. For production, use a proper ASGI server like Uvicorn or Hypercorn.
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000) 