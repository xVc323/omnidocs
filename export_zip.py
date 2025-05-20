import os
import zipfile
import time
import sys
import argparse
import boto3
from botocore.exceptions import NoCredentialsError, PartialCredentialsError, ClientError
from dotenv import load_dotenv
import shutil

# Load environment variables from .env file if it exists (for local development)
# This should be one of the first things done.
load_dotenv()

def get_r2_client():
    """Initializes and returns an S3 client configured for R2."""
    try:
        r2_account_id = os.environ['R2_ACCOUNT_ID']
        r2_access_key_id = os.environ['R2_ACCESS_KEY_ID']
        r2_secret_access_key = os.environ['R2_SECRET_ACCESS_KEY']
    except KeyError as e:
        print(f"Error: Missing R2 environment variable: {e}", file=sys.stderr)
        sys.exit(1)

    endpoint_url = f'https://{r2_account_id}.r2.cloudflarestorage.com'

    s3_client = boto3.client(
        service_name='s3',
        endpoint_url=endpoint_url,
        aws_access_key_id=r2_access_key_id,
        aws_secret_access_key=r2_secret_access_key,
        region_name='auto'
    )
    return s3_client

def upload_to_r2(s3_client, local_file_path, bucket_name, object_name=None):
    """Uploads a file to an R2 bucket and returns the object name."""
    if object_name is None:
        object_name = os.path.basename(local_file_path)
    
    file_extension = os.path.splitext(object_name)[1].lower()
    extra_args = {}

    # Set ContentType and ContentDisposition for browser behavior
    if file_extension == '.md':
        extra_args['ContentType'] = 'text/markdown; charset=UTF-8'
        extra_args['ContentDisposition'] = f'attachment; filename="{os.path.basename(object_name)}"'
    elif file_extension == '.zip':
        extra_args['ContentType'] = 'application/zip'
        extra_args['ContentDisposition'] = f'attachment; filename="{os.path.basename(object_name)}"'
    else: # Fallback for other types if any, ensure download
        extra_args['ContentType'] = 'application/octet-stream'
        extra_args['ContentDisposition'] = f'attachment; filename="{os.path.basename(object_name)}"'

    try:
        s3_client.upload_file(local_file_path, bucket_name, object_name, ExtraArgs=extra_args)
        return object_name
    except FileNotFoundError:
        print(f"Error: Local file not found: {local_file_path}", file=sys.stderr)
        sys.exit(1)
    except (NoCredentialsError, PartialCredentialsError):
        print("Error: AWS credentials not found or incomplete for R2.", file=sys.stderr)
        sys.exit(1)
    except ClientError as e:
        print(f"Error uploading to R2: {e}", file=sys.stderr)
        sys.exit(1)
    return None

def create_zip_archive(output_base_dir):
    """Creates a zip archive of docs and all_docs.md, returns its local path."""
    docs_dir = 'docs'
    output_dir_name = os.path.join(output_base_dir, 'temp_outputs')
    if not os.path.isdir(output_dir_name):
        os.makedirs(output_dir_name, exist_ok=True)

    timestamp = int(time.time())
    output_zip_basename = f"omni_docs_export_{timestamp}.zip"
    local_zip_path = os.path.join(output_dir_name, output_zip_basename)
    
    files_to_zip = []
    if not os.path.isdir(docs_dir):
        print(f"Error: Documentation directory '{docs_dir}' not found.", file=sys.stderr)
        return None

    for fname in os.listdir(docs_dir):
        if fname.endswith('.md') and os.path.isfile(os.path.join(docs_dir, fname)):
            files_to_zip.append(os.path.join(docs_dir, fname))
    
    order_txt = os.path.join(docs_dir, 'order.txt')
    if os.path.isfile(order_txt):
        files_to_zip.append(order_txt)
    
    all_docs_md_path = 'all_docs.md' 
    if os.path.isfile(all_docs_md_path):
        files_to_zip.append(all_docs_md_path)

    if not files_to_zip:
        print("No files to zip.", file=sys.stderr)
        return None

    try:
        with zipfile.ZipFile(local_zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for fpath in files_to_zip:
                if fpath.startswith(docs_dir + os.sep):
                    arcname = os.path.relpath(fpath, start=os.path.dirname(docs_dir))
                elif fpath == all_docs_md_path:
                    arcname = os.path.basename(all_docs_md_path)
                else:
                    arcname = os.path.basename(fpath)
                zipf.write(fpath, arcname)
        return local_zip_path
    except Exception as e:
        print(f"Error creating ZIP file: {e}", file=sys.stderr)
        return None

def main():
    parser = argparse.ArgumentParser(description="Create a ZIP archive or upload a specific file to R2.")
    parser.add_argument("--file", type=str, help="Path to a specific file to upload to R2 directly.")
    parser.add_argument("--temp-dir-base", type=str, default=os.getcwd(), help="Base directory for temporary outputs.")
    args = parser.parse_args()

    try:
        bucket_name = os.environ['R2_BUCKET_NAME']
        r2_public_domain = os.environ.get('R2_PUBLIC_DOMAIN')
    except KeyError as e:
        print(f"Error: Missing R2 environment variable: {e}", file=sys.stderr)
        sys.exit(1)

    s3_client = get_r2_client()
    
    file_to_upload_path = None
    object_name_for_r2 = None

    if args.file:
        if not os.path.isfile(args.file):
            print(f"Error: Specified file not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        file_to_upload_path = args.file
        object_name_for_r2 = os.path.basename(args.file)
    else:
        local_zip_path = create_zip_archive(args.temp_dir_base)
        if not local_zip_path:
            print("Failed to create ZIP archive.", file=sys.stderr)
            sys.exit(1)
        file_to_upload_path = local_zip_path
        object_name_for_r2 = os.path.basename(local_zip_path) 

    uploaded_object_name = upload_to_r2(s3_client, file_to_upload_path, bucket_name, object_name_for_r2)

    if uploaded_object_name:
        if r2_public_domain:
            clean_domain = r2_public_domain.strip('/')
            clean_object_name = uploaded_object_name.lstrip('/')
            public_url = f"https://{clean_domain}/{clean_object_name}"
            print(public_url)
        else:
            print(f"UPLOAD_SUCCESS:{bucket_name}/{uploaded_object_name}", file=sys.stdout)
    else:
        print("Upload failed.", file=sys.stderr)
        sys.exit(1)

    if not args.file and file_to_upload_path and os.path.exists(file_to_upload_path):
        try:
            os.remove(file_to_upload_path)
        except OSError as e:
            print(f"Warning: Could not clean up temp file {file_to_upload_path}: {e}", file=sys.stderr)

    # After successful upload, clean up docs/ and all_docs.md
    if uploaded_object_name:
        docs_dir = os.path.join(os.path.dirname(__file__), 'docs')
        all_docs_path = os.path.join(os.path.dirname(__file__), 'all_docs.md')
        if os.path.exists(docs_dir):
            shutil.rmtree(docs_dir)
        if os.path.exists(all_docs_path):
            os.remove(all_docs_path)

if __name__ == '__main__':
    main() 