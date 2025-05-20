import time
import os
import re
import json
import zipfile
from urllib.parse import urljoin, urlparse, unquote
from collections import deque
from datetime import datetime, timezone, timedelta
import mimetypes
from email.utils import parsedate_to_datetime
import shutil
import asyncio
import logging
import subprocess
import tempfile
import uuid
from typing import Dict, List, Optional, Tuple, Union

import requests
from bs4 import BeautifulSoup, NavigableString, Tag, Comment # Added Comment
import tldextract
import pypandoc
import html2text
import boto3 
from botocore.exceptions import NoCredentialsError, PartialCredentialsError, ClientError
from dotenv import load_dotenv

from celery_app import celery_app
from celery import current_task, states
from celery.exceptions import Ignore
# from typing import Optional, Dict, Any # Already imported via typing

# Load environment variables for R2, primarily for local Celery worker testing
load_dotenv()

# Silence gevent threading warnings
logging.getLogger("gevent.threading").setLevel(logging.ERROR)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Toggle this variable to strictly enforce page limits across both phases ===
STRICT_PAGE_LIMIT_ENFORCEMENT = False
# ===========================================================================

# --- Constants from crawler.py (adapted) ---
USER_AGENT = "OmniDocsCrawler/0.4-Celery-Clean (respectful-crawler; https://github.com/xvc323/omnidocs)" # Version bump
SCOPE_PATH_SEGMENT_DEPTH = 2 
MAIN_CONTENT_SELECTORS = [
    'div.theme-doc-markdown.markdown', # Docusaurus
    'article.md-content__inner',      # Material for MkDocs
    'main .content',                  # Common pattern
    'div[role="main"]',               # Common ARIA role
    'main',                           # HTML5 main tag
    'article',                        # HTML5 article tag
    '#content',                       # Common ID
    '.content',                       # Common class
    '#main-content',                  # Common ID
    '.main-content',                  # Common class
    'body'                            # Fallback
]
DOCS_SUBDIR_NAME = "docs" 
ALL_DOCS_FILENAME = "all_docs.md"
ORDER_FILENAME = "order.txt"
ZIP_FILENAME_PREFIX = "omni_docs_export_"

# --- R2 Object Expiration Settings ---
R2_OBJECT_EXPIRATION_HOURS = 1  # Files will be automatically deleted after this many hours

# --- Adaptive Delay Parameters ---
INITIAL_REQUEST_DELAY_SECONDS = 0.75
MIN_REQUEST_DELAY_SECONDS = 0.25
MAX_REQUEST_DELAY_SECONDS = 5.0
RETRY_AFTER_DEFAULT_SECONDS = 8.0
SUCCESSFUL_HTML_PAGES_TO_SHORTEN_DELAY = 5
DELAY_DECREMENT_STEP_SECONDS = 0.25
PENALTY_BOX_DURATION_SECONDS = 20.0
PENALTY_BOX_REQUEST_COUNT = 5

# --- R2 Utility Functions (adapted from export_zip.py) ---
def get_r2_client(task_instance):
    """Initializes and returns an S3 client configured for R2."""
    try:
        r2_account_id = os.environ['R2_ACCOUNT_ID']
        r2_access_key_id = os.environ['R2_ACCESS_KEY_ID']
        r2_secret_access_key = os.environ['R2_SECRET_ACCESS_KEY']
    except KeyError as e:
        task_instance.update_state(state=states.FAILURE, meta={'status': f'Configuration error: Missing R2 env var: {e}', 'error': f'Missing R2 env var: {e}'})
        raise Ignore()

    endpoint_url = f'https://{r2_account_id}.r2.cloudflarestorage.com'
    try:
        s3_client = boto3.client(
            service_name='s3',
            endpoint_url=endpoint_url,
            aws_access_key_id=r2_access_key_id,
            aws_secret_access_key=r2_secret_access_key,
            region_name='auto' # Or your specific region
        )
        return s3_client
    except Exception as e:
        task_instance.update_state(state=states.FAILURE, meta={'status': f'Configuration error: Could not create R2 client: {e}', 'error': f'R2 client init failed: {e}'})
        raise Ignore()

def upload_to_r2(s3_client, local_file_path, bucket_name, object_name, task_instance):
    """Uploads a file to an R2 bucket and returns the object name."""
    file_extension = os.path.splitext(object_name)[1].lower()
    extra_args = {}

    if file_extension == '.md':
        extra_args['ContentType'] = 'text/markdown; charset=UTF-8'
        extra_args['ContentDisposition'] = f'attachment; filename="{os.path.basename(object_name)}"'
    elif file_extension == '.zip':
        extra_args['ContentType'] = 'application/zip'
        extra_args['ContentDisposition'] = f'attachment; filename="{os.path.basename(object_name)}"'
    else:
        extra_args['ContentType'] = 'application/octet-stream' # Default
        extra_args['ContentDisposition'] = f'attachment; filename="{os.path.basename(object_name)}"'

    # Add metadata for expiration time
    expiration_time = (datetime.now() + timedelta(hours=R2_OBJECT_EXPIRATION_HOURS)).isoformat()
    extra_args['Metadata'] = {'expiration_time': expiration_time}

    try:
        task_instance.update_state(state='PROGRESS', meta={'status': f'Uploading {os.path.basename(local_file_path)} to R2 as {object_name}'})
        s3_client.upload_file(local_file_path, bucket_name, object_name, ExtraArgs=extra_args)
        return object_name
    except FileNotFoundError:
        task_instance.update_state(state=states.FAILURE, meta={'status': f'Upload error: Local file not found: {local_file_path}', 'error': 'Local file missing for upload'})
        raise Ignore()
    except (NoCredentialsError, PartialCredentialsError):
        task_instance.update_state(state=states.FAILURE, meta={'status': 'Upload error: R2 credentials missing or incomplete.', 'error': 'R2 credentials error'})
        raise Ignore()
    except ClientError as e:
        task_instance.update_state(state=states.FAILURE, meta={'status': f'Upload error: R2 client error: {e}', 'error': f'R2 client error: {e}'})
        raise Ignore()
    except Exception as e: # Catch any other exception
        task_instance.update_state(state=states.FAILURE, meta={'status': f'Upload error: Unexpected error during R2 upload: {e}', 'error': f'Unexpected R2 upload error: {e}'})
        raise Ignore()

def delete_from_r2(s3_client, bucket_name, object_name):
    """Deletes an object from an R2 bucket."""
    try:
        s3_client.delete_object(Bucket=bucket_name, Key=object_name)
        return True
    except Exception as e:
        logger.error(f"Error deleting object {object_name} from bucket {bucket_name}: {e}")
        return False

# --- Helper Functions (largely from crawler.py) ---

def safe_filename(url, job_output_dir, for_ordering=False):
    parsed = urlparse(url)
    path = parsed.path.strip('/')
    if not path or path.endswith('/'): 
        path = os.path.join(path, 'index')
    
    path_parts = [part for part in path.split('/') if part]
    
    decoded_parts = []
    for part in path_parts:
        try:
            decoded_part = unquote(part)
            sane_part = re.sub(r'[^a-zA-Z0-9_.-]', '_', decoded_part)
            # Limit length of individual parts to prevent excessively long filenames/paths
            sane_part = sane_part[:50] 
            decoded_parts.append(sane_part)
        except Exception:
            decoded_parts.append(re.sub(r'[^a-zA-Z0-9_.-]', '_', part)[:50])

    if not decoded_parts:
        filename_base = 'index'
    else:
        filename_base = '_'.join(decoded_parts)
        # Limit overall base length
        filename_base = filename_base[:150]


    if for_ordering: 
        return filename_base + '.md'
    
    sub_path_parts = decoded_parts[:-1] if len(decoded_parts) > 1 else []
    final_name_part = decoded_parts[-1] if decoded_parts else 'index'

    target_dir = os.path.join(job_output_dir, DOCS_SUBDIR_NAME, *sub_path_parts)
    os.makedirs(target_dir, exist_ok=True)
    
    return os.path.join(target_dir, final_name_part + '.md')


def html_tables_to_md(md_content):
    # This function is called if Pandoc outputs HTML tables or fails to convert them.
    def table_replacer(match):
        table_html = match.group(0)
        # Pre-clean table HTML for html2text
        soup = BeautifulSoup(table_html, 'html.parser')
        # Remove a, span tags from within tables to simplify for html2text
        for tag_type in ['a', 'span']:
            for tag in soup.find_all(tag_type):
                tag.unwrap()
        # Remove common attributes that might confuse html2text
        for tag in soup.find_all(True):
            for attr in ['class', 'style', 'id', 'role', 'width', 'cellspacing', 'cellpadding', 'border', 'valign', 'align', 'bgcolor', 'data-column-id']:
                if attr in tag.attrs:
                    del tag[attr]
        
        cleaned_table_html = str(soup)

        h = html2text.HTML2Text()
        h.body_width = 0 # No line wrapping
        h.ignore_links = True # Links in tables often noisy
        h.ignore_images = True
        h.ignore_emphasis = False
        h.bypass_tables = False # Crucial: tell html2text to handle tables
        h.unicode_snob = True
        h.escape_snob = True
        try:
            md_table = h.handle(cleaned_table_html)
            # Post-process html2text table output for common issues
            md_table = re.sub(r'\n{2,}', '\n', md_table) # Reduce multiple newlines within table
            md_table = re.sub(r'(\| ){2,}', '| ', md_table) # Fix extra spaces after pipes
            md_table = re.sub(r'( \|){2,}', ' |', md_table) # Fix extra spaces before pipes
            return md_table.strip()
        except Exception as e:
            logger.warning(f"html2text failed to convert table: {e}. Table HTML: {table_html[:200]}")
            return "[html2text failed to convert table]"
            
    # Match only if it seems like an actual HTML table, not just words "table"
    return re.sub(r'<table([\s\S]*?)</table>', table_replacer, md_content, flags=re.IGNORECASE)

def clean_markdown_artifacts(md_content):
    # This runs after Pandoc/html2text but before the final deep clean.
    # Catches common Pandoc/conversion artifacts.
    md_content = re.sub(r'^:+.*$', '', md_content, flags=re.MULTILINE) # Definition/caption lines with colon
    md_content = re.sub(r'(\{\.[\w\d\s\.\-⚙]+(?:role=\"[^\"]*\")?(?:style=\"[^\"]*\")?(?:testid=\"[^\"]*\")?(?:tabindex=\"[^\"]*\")?[^\}]*\})', '', md_content)
    md_content = re.sub(r'(\]\{\.[\w\d\s\.\-⚙]+[^\}]*\})', ']', md_content)
    md_content = re.sub(r'^-+\|[-|]+$', '', md_content, flags=re.MULTILINE) # Non-standard table separators
    md_content = re.sub(r' (class|id|style|data-\w+)=""', '', md_content)
    md_content = re.sub(r'\[\[([^\]]+)\]\]', r'[\1]', md_content) # Fix double-bracketed links
    return md_content

def final_html_strip_and_prettify(md_text: str) -> str:
    # 1. Initial text normalizations
    md_text = md_text.replace('\u200B', '').replace('â€‹', '').replace('\u00A0', ' ') # Zero-width, non-breaking spaces
    md_text = re.sub(r'^\s*#', '#', md_text, flags=re.MULTILINE) # Remove leading spaces before headings
    md_text = re.sub(r'#\s*$', '', md_text, flags=re.MULTILINE) # Remove trailing spaces after headings (if line ends there)
    md_text = re.sub(r'^\s*>\s', '> ', md_text, flags=re.MULTILINE) # Normalize blockquote spacing

    # 2. Attempt to parse as HTML to catch stubborn tags (this is after Pandoc/html2text)
    # This is for removing any *remaining* HTML tags that shouldn't be in clean Markdown.
    # It's less about converting HTML to MD here, and more about stripping unwanted HTML structures.
    try:
        # Only apply BeautifulSoup if there's a sign of HTML tags still present.
        # This avoids unnecessary parsing of already clean Markdown.
        if '<' in md_text and '>' in md_text and re.search(r'<[a-zA-Z][^>]*>', md_text):
            soup = BeautifulSoup(md_text, 'html.parser')
            
            tags_to_aggressively_remove_or_unwrap = [
                'div', 'span', 'font', 'section', 'article', 'header', 'footer', 'aside', 
                'nav', 'figure', 'figcaption', 'details', 'summary', 'button', 'form', 
                'input', 'label', 'textarea', 'select', 'option', 'iframe', 'canvas',
                'map', 'area', 'audio', 'video', 'source', 'track', 'embed', 'object', 'param'
            ]
            for tag_name in tags_to_aggressively_remove_or_unwrap:
                for t in soup.find_all(tag_name):
                    # Prefer unwrapping if it contains meaningful children, otherwise get text or decompose
                    if t.find_all(True, recursive=False) or t.get_text(strip=True): # Has children or text
                        t.unwrap() # Try unwrapping first
                    else:
                        t.decompose()
            
            # Handle remaining <img> tags specifically if they are raw HTML
            for img in soup.find_all('img'):
                alt = img.get('alt', 'Image').strip()
                src = img.get('src', '')
                if src and not src.startswith('data:'):
                    img.replace_with(NavigableString(f"![{alt}]({src})"))
                elif alt:
                    img.replace_with(NavigableString(f"[Image: {alt}]"))
                else:
                    img.replace_with(NavigableString("[Image placeholder]"))

            # Handle remaining <a> tags specifically
            for a_tag in soup.find_all('a'):
                href = a_tag.get('href')
                text = a_tag.get_text(strip=True)
                if href and not href.startswith('javascript:'):
                    if text:
                        a_tag.replace_with(NavigableString(f"[{text}]({href})"))
                    else: # No text, use href as text or a placeholder
                        link_text_candidate = urlparse(href).path.split('/')[-1] or "link"
                        a_tag.replace_with(NavigableString(f"[{link_text_candidate}]({href})"))
                elif text: # No valid href, but has text
                    a_tag.replace_with(NavigableString(text))
                else: # No href, no text
                    a_tag.decompose()
            
            # After specific tag handling, if we want to ensure NO HTML remains:
            # cleaned_md_string = soup.get_text(separator='\n') # This is very aggressive.
            # A less aggressive approach is to take str(soup) and let regex cleanups handle MD syntax.
            md_text = str(soup)
    except Exception as e:
        logger.debug(f"BeautifulSoup parsing in final_html_strip_and_prettify failed or was skipped: {e}")
        # md_text remains as is if it's not parseable as HTML (which is good)
    
    cleaned_md_string = md_text

    # 3. Regex-based cleanups
    # Remove Pandoc/CommonMark attributes
    cleaned_md_string = re.sub(r'\{\s*(#[\w\-]+|\.[\w\-]+|[\w\-]+=.+?)\s*\}', '', cleaned_md_string)
    cleaned_md_string = re.sub(r'(\]\{\.[\w\d\s\.\-⚙]+[^\}]*\})', ']', cleaned_md_string)
    
    # Remove ::: blocks (common in some MD extensions like admonitions/alerts)
    cleaned_md_string = re.sub(r'^:::.+\n([\s\S]*?)\n:::(\s*\n|$)', r'\1\n', cleaned_md_string, flags=re.MULTILINE)
    cleaned_md_string = re.sub(r'^:::.*$', '', cleaned_md_string, flags=re.MULTILINE) # Single line :::

    # Clean code block fences
    cleaned_md_string = re.sub(r'```\s*\{[^\}]+\}', '```', cleaned_md_string) # ``` { .lang } -> ```lang
    cleaned_md_string = re.sub(r'```([\w\+\-]+)\s*\n', r'```\1\n', cleaned_md_string) # Ensure lang is on same line
    cleaned_md_string = re.sub(r'```\s*(\n|$)', r'```\1', cleaned_md_string) # Remove trailing spaces on ``` line


    # Clean up empty HTML comments
    cleaned_md_string = re.sub(r'<!--(.*?)-->', lambda m: f"[Comment: {m.group(1).strip()}]" if m.group(1).strip() else "", cleaned_md_string, flags=re.DOTALL)
    cleaned_md_string = re.sub(r'\[Comment:\s*\]', '', cleaned_md_string)


    # Stripe specific cleanups (or similar patterns) for `!` prefixes
    cleaned_md_string = re.sub(r'^(#+\s+)!\s*', r'\1', cleaned_md_string, flags=re.MULTILINE) # For "#### !"
    cleaned_md_string = re.sub(r'^(\s*[\*\-]\s*|\s*>\s*|\s*\d+\.\s*)!\s*', r'\1', cleaned_md_string, flags=re.MULTILINE) # For list items, blockquotes
    # For paragraph-like lines starting with !, be cautious. Only if it's clearly not an image.
    cleaned_md_string = re.sub(r'^(?!\s*!\[)!\s*(?=[A-Za-z0-9])', '', cleaned_md_string, flags=re.MULTILINE) # !Text -> Text, but not ![Alt]

    # Remove empty links or links with only spaces/special chars as text
    cleaned_md_string = re.sub(r'\[\s*\]\([^\)]+\)', '', cleaned_md_string)
    cleaned_md_string = re.sub(r'\[[^a-zA-Z0-9]*?\]\(([^)]+)\)', r'<\1>', cleaned_md_string) # If text is just symbols, show URL


    # Remove image data URIs (should be handled earlier, but as a fallback)
    cleaned_md_string = re.sub(r'!\[[^\]]*\]\(data:image/[^;]+;base64,[^)]+\)', '[Embedded Image Removed]', cleaned_md_string)
    
    # Normalize excessive newlines
    cleaned_md_string = re.sub(r'\n{3,}', '\n\n', cleaned_md_string)
    
    # Remove leading/trailing whitespace on each line, then re-normalize blank lines
    cleaned_md_string = "\n".join([line.strip() for line in cleaned_md_string.splitlines()])
    cleaned_md_string = re.sub(r'\n{3,}', '\n\n', cleaned_md_string) # Again, after strip
    
    # Remove invisible characters again
    cleaned_md_string = cleaned_md_string.replace('\u200B', '').replace('â€‹', '')
    
    # Clean up duplicate image placeholders
    placeholder_patterns = [
        r'(\[Embedded Image(?: Removed|: [^\]]*)?\]\s*){2,}',
        r'(\[Missing Image Source(?: Removed|: [^\]]*)?\]\s*){2,}',
        r'(\[Image placeholder\]\s*){2,}'
    ]
    for pattern in placeholder_patterns:
        cleaned_md_string = re.sub(pattern, r'\1', cleaned_md_string)
    
    # Remove numbered references like [1], [2] if they are not part of a link
    cleaned_md_string = re.sub(r'\s\[\d+\](?!\()', '', cleaned_md_string) 
    
    # Remove reference-style link definitions at the end of the document (Pandoc might still produce these)
    cleaned_md_string = re.sub(r'^\s*\[[^\]]+\]:\s+.*$', '', cleaned_md_string, flags=re.MULTILINE)
    
    # Remove lines that are solely non-alphanumeric, often remnants of borders or separators
    def clean_symbol_lines_func(text):
        lines = text.split('\n')
        processed_lines = []
        for line in lines:
            stripped_line = line.strip()
            # Keep standard Markdown horizontal rules and code fences
            if re.fullmatch(r'-{3,}|_{3,}|\*{3,}', stripped_line) or stripped_line.startswith('```'):
                processed_lines.append(line)
            # Keep lines with at least one alphanumeric char, or common MD punctuation for links/images/lists
            elif re.search(r'[a-zA-Z0-9]', stripped_line) or \
                 re.search(r'[!#*\[\]\(\)_]', stripped_line) and not re.fullmatch(r'[^a-zA-Z0-9\s]+', stripped_line) : # Avoid lines of ONLY symbols unless HR
                processed_lines.append(line)
            elif not stripped_line: # Keep empty lines (for spacing)
                 processed_lines.append(line)
        return '\n'.join(processed_lines)

    cleaned_md_string = clean_symbol_lines_func(cleaned_md_string)
    # Re-normalize newlines after potential line removals
    cleaned_md_string = re.sub(r'\n{3,}', '\n\n', cleaned_md_string)

    return cleaned_md_string.strip() + '\n'


def get_path_prefix_parts(url, depth):
    path_parts = urlparse(url).path.strip('/').split('/')
    return path_parts[:depth] if path_parts[0] else [] 

def in_scope(url, seed_host, seed_scope_prefix_parts_list, include_prefixes=None, exclude_regexes=None):
    parsed = urlparse(url)
    if parsed.hostname != seed_host: return False
    
    current_path = parsed.path
    # Apply exclude_regexes first
    if exclude_regexes:
        for pattern in exclude_regexes:
            if re.search(pattern, url): return False 
            if re.search(pattern, current_path): return False


    if include_prefixes:
        path_included = False
        for prefix in include_prefixes:
            # Ensure prefix matching is robust (e.g. /docs vs /docs/page)
            # A common way is to ensure path starts with prefix and is followed by / or end of string
            # Or, if prefix itself ends with /, then path must start with it.
            if prefix.endswith('/'):
                if current_path.startswith(prefix):
                    path_included = True
                    break
            else: # prefix doesn't end with / e.g. /docs
                if current_path == prefix or current_path.startswith(prefix + '/'):
                    path_included = True
                    break
        if not path_included: return False 
        return True 

    # Default scoping if no include_prefixes
    current_path_parts = current_path.strip('/').split('/')
    if current_path_parts[0] == '': current_path_parts = [] 
    
    is_scoped_by_prefix = False
    for seed_prefix_parts in seed_scope_prefix_parts_list:
        if not seed_prefix_parts: # Empty prefix_parts means root scope (e.g. from example.com/)
            is_scoped_by_prefix = True # Everything on this host is in scope by this rule
            break
        if len(current_path_parts) >= len(seed_prefix_parts) and \
           current_path_parts[:len(seed_prefix_parts)] == seed_prefix_parts:
            is_scoped_by_prefix = True
            break
    return is_scoped_by_prefix


def is_html_url(url, headers):
    content_type = headers.get('Content-Type', '').lower()
    if 'text/html' in content_type: return True
    
    parsed_path = urlparse(url).path
    if parsed_path == '' or parsed_path.endswith('/'): return True 
    
    ext = os.path.splitext(parsed_path)[1].lower()
    html_exts = ['.html', '.htm', '.asp', '.aspx', '.php', '.jsp', ''] # Added empty ext for clean URLs
    non_html_mimetypes = ['application/pdf', 'image/', 'application/zip', 'text/plain', 'text/css', 'application/javascript', 'application/json', 'application/xml']
    
    if any(ct_part in content_type for ct_part in non_html_mimetypes): return False
    if ext in html_exts: return True
    
    guessed_type, _ = mimetypes.guess_type(url)
    if guessed_type and guessed_type.startswith('text/') and guessed_type != 'text/plain':
        return True
        
    # If content_type is missing or generic (octet-stream) but URL has no extension or HTML-like extension
    if (not content_type or 'application/octet-stream' in content_type) and (not ext or ext in html_exts):
        return True # Cautiously assume HTML for extensionless URLs if not explicitly non-HTML

    return False


def fetch_page_content(url, session, task_instance, current_delay):
    try:
        task_instance.update_state(state='PROGRESS', meta={'status': f'Fetching: {url}', 'current_url': url})
        time.sleep(current_delay) 
        response = session.get(url, timeout=20) 
        response.raise_for_status() 
        return response.text, response.headers, response.status_code, None 
    except requests.exceptions.HTTPError as e:
        return None, e.response.headers if e.response else {}, e.response.status_code if e.response else 0, e
    except requests.exceptions.RequestException as e:
        return None, {}, 0, e 

def extract_nav_links(html_content, base_url, seed_host, seed_scope_prefixes, include_prefixes, exclude_regexes):
    soup = BeautifulSoup(html_content, 'html.parser')
    links = set()
    nav_selectors = ['nav', '.sidebar', '.toc', '.menu', '#TableOfContents', 
                     'div[class*="nav"]', 'div[id*="nav"]', 
                     'div[class*="menu"]', 'div[id*="menu"]',
                     'div[class*="toc"]', 'div[id*="toc"]',
                     'div[class*="sidebar"]', 'div[id*="sidebar"]',
                     'ul[class*="nav"]', 'ol[class*="nav"]',
                     'aside[class*="nav"]', 'aside[id*="nav"]' # Added aside variants
                     ] 
    nav_container = None
    for selector in nav_selectors:
        elements = soup.select(selector) # Use select to find multiple if they exist
        if elements:
            # Simple heuristic: pick the largest navigation container if multiple are found
            # or just use the first one. For now, use all found.
            for el in elements:
                 for a_tag in el.find_all('a', href=True):
                    href = a_tag['href']
                    full_url = urljoin(base_url, href)
                    parsed_full_url = urlparse(full_url)
                    # Clean fragments and common tracking queries, but keep meaningful queries
                    # For now, just remove fragment. Query removal can be too aggressive.
                    full_url_cleaned = parsed_full_url._replace(fragment="").geturl()

                    if in_scope(full_url_cleaned, seed_host, seed_scope_prefixes, include_prefixes, exclude_regexes):
                        links.add(full_url_cleaned)
            # If we found links in specific nav containers, we might not need to scan the whole doc.
            # However, some sites have links scattered. For now, continue to scan whole doc.

    # Fallback or supplement with links from the entire document if no specific nav found or to be thorough
    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        # Skip mailto, tel, etc.
        if href.startswith(('mailto:', 'tel:', 'javascript:')):
            continue
        full_url = urljoin(base_url, href)
        parsed_full_url = urlparse(full_url)
        full_url_cleaned = parsed_full_url._replace(fragment="").geturl()

        if in_scope(full_url_cleaned, seed_host, seed_scope_prefixes, include_prefixes, exclude_regexes):
            links.add(full_url_cleaned)
            
    return list(links)


def convert_to_markdown_pypandoc(html_content: str, url: str, task_instance) -> str:
    task_instance.update_state(state='PROGRESS', meta={'status': f'Converting (Pandoc): {url}', 'current_url': url})
    
    # --- Enhanced HTML Pre-processing ---
    try:
        soup = BeautifulSoup(html_content, 'html.parser')

        # 1. Remove scripts, styles, SVGs, comments, and other non-content tags
        for element_type in ['script', 'style', 'svg', 'noscript', 'meta', 'link', 'iframe', 'embed', 'object', 'param', 'source', 'track', 'map', 'area', 'canvas', 'audio', 'video']:
            for element in soup.find_all(element_type):
                element.decompose()
        
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract() # Use extract() which returns the comment, then it's garbage collected

        # 2. Select main content area
        main_content_el = None
        for selector in MAIN_CONTENT_SELECTORS:
            main_content_el = soup.select_one(selector)
            if main_content_el:
                break
        
        # Work on a copy of the selected content or the whole soup
        content_soup = BeautifulSoup(str(main_content_el), 'html.parser') if main_content_el else soup
        
        # 3. Further clean the selected content_soup
        # Remove common semantic but often noisy tags if not part of core content (e.g. site-wide headers/footers)
        # These are typically outside the `main_content_el` if selectors are good.
        # If `main_content_el` is `body`, then these become important.
        # For now, assuming MAIN_CONTENT_SELECTORS are decent.
        tags_to_remove_completely = ['header', 'footer', 'aside', 'nav'] 
        if not main_content_el or main_content_el.name == 'body': # Be more aggressive if we're processing whole body
            for tag_name in tags_to_remove_completely:
                for tag in content_soup.find_all(tag_name):
                    tag.decompose()

        # Unwrap tags that are often just for styling or simple grouping
        tags_to_unwrap = ['div', 'span', 'font', 'section', 'article', 'figure', 'figcaption', 'details', 'summary']
        for tag_name in tags_to_unwrap:
            for tag in content_soup.find_all(tag_name):
                tag.unwrap()

        # 4. Attribute stripping
        for tag in content_soup.find_all(True):
            attrs_to_keep = {'a': ['href', 'title'], 'img': ['src', 'alt', 'title']}
            allowed_attrs = attrs_to_keep.get(tag.name, ['title']) 
            current_attrs = list(tag.attrs.keys())
            for attr in current_attrs:
                if attr not in allowed_attrs:
                    del tag[attr]
        
        # 5. Image specific pre-processing
        for img in content_soup.find_all('img'):
            src = img.get('src', '')
            alt = img.get('alt', '').strip()
            placeholder = None
            if src.startswith('data:image'):
                placeholder = f"[Embedded Image: {alt}]" if alt else "[Embedded Image Removed]"
            elif not src:
                placeholder = f"[Missing Image Source: {alt}]" if alt else "[Missing Image Source]"
            elif not alt: # Add default alt if missing and src is valid
                 img['alt'] = 'Image' # Keep the img tag for Pandoc if src is valid
            
            if placeholder:
                 img.replace_with(NavigableString(placeholder))

        content_to_convert = str(content_soup)

        # Pandoc conversion
        # Removed '--reference-links'
        # Added '--strip-comments' (Pandoc's own)
        # Added '--markdown-headings=atx' (if not already default for gfm)
        md = pypandoc.convert_text(content_to_convert, 'gfm', format='html',
                                   extra_args=['--wrap=none', 
                                               '--markdown-headings=atx',
                                               '--no-highlight', # Keep no-highlight
                                               '--email-obfuscation=none',
                                               '--strip-comments'
                                               ])
    except Exception as e_pandoc_prep:
        logger.error(f"Error during Pandoc pre-processing for {url}: {e_pandoc_prep}")
        # Fallback will be triggered by the outer try-except
        raise # Re-raise to trigger the main Pandoc exception handling

    # Post-Pandoc processing
    # If Pandoc outputs tables as HTML (it shouldn't with GFM, but as a safeguard)
    if '<table' in md: 
        md = html_tables_to_md(md)
    md = clean_markdown_artifacts(md) # Initial regex cleanup
    md = final_html_strip_and_prettify(md) # Thorough final cleanup
    return md

    # Fallback to html2text (outer try-except in the calling function will handle this)
    # The structure of the original function handles the fallback. We just need to ensure
    # the Pandoc part raises an exception if it fails critically.

# The main convert_to_markdown_pypandoc now contains the primary logic.
# The fallback part in the original code was slightly different. Let's adjust the main function structure.

def convert_html_to_markdown(html_content, url, task_instance):
    # Primary attempt: Pandoc
    try:
        return convert_to_markdown_pypandoc(html_content, url, task_instance)
    except Exception as e_pandoc:
        task_instance.update_state(state='PROGRESS', meta={'status': f'Pandoc failed for {url}, trying html2text. Error: {str(e_pandoc)}', 'current_url': url})
        logger.warning(f"Pandoc conversion failed for {url}: {e_pandoc}. Falling back to html2text.")
        
        # Fallback: html2text
        try:
            h = html2text.HTML2Text()
            h.body_width = 0
            h.ignore_links = False 
            h.ignore_images = False 
            h.ignore_emphasis = False
            h.bypass_tables = False 
            h.unicode_snob = True
            h.strong_mark = "**"
            h.emphasis_mark = "_"
            h.escape_snob = True
            h.skip_internal_links = True
            h.inline_links = True
            h.protect_links = True
            h.mark_code = True
            
            # Pre-process HTML for html2text (similar to Pandoc's, but might be simpler)
            soup_for_h2t = BeautifulSoup(html_content, 'html.parser')
            
            main_content_h2t_el = None
            for selector in MAIN_CONTENT_SELECTORS:
                main_content_h2t_el = soup_for_h2t.select_one(selector)
                if main_content_h2t_el:
                    break
            
            h2t_content_soup = BeautifulSoup(str(main_content_h2t_el), 'html.parser') if main_content_h2t_el else soup_for_h2t
            
            for element_type in ['script', 'style', 'svg', 'noscript', 'meta', 'link', 'iframe', 'header', 'footer', 'aside', 'nav']:
                for element in h2t_content_soup.find_all(element_type):
                    element.decompose()
            for comment in h2t_content_soup.find_all(string=lambda text: isinstance(text, Comment)):
                comment.extract()

            for tag in h2t_content_soup.find_all(True):
                for attr in list(tag.attrs): # More aggressive attribute removal for html2text
                    if attr not in ['href', 'src', 'alt', 'title']: # Keep only very essential
                        del tag[attr]

            for img in h2t_content_soup.find_all('img'):
                src = img.get('src', '')
                alt = img.get('alt', '').strip()
                placeholder = None
                if src.startswith('data:image'):
                    placeholder = f"[Embedded Image: {alt}]" if alt else "[Embedded Image Removed]"
                elif not src:
                    placeholder = f"[Missing Image Source: {alt}]" if alt else "[Missing Image Source]"
                if placeholder:
                    img.replace_with(NavigableString(placeholder))

            content_to_convert_h2t = str(h2t_content_soup)
            
            md = h.handle(content_to_convert_h2t)
            md = clean_markdown_artifacts(md) 
            md = final_html_strip_and_prettify(md)
            return md
        except Exception as e_html2text:
            task_instance.update_state(state='PROGRESS', meta={'status': f'html2text also failed for {url}. Error: {str(e_html2text)}', 'current_url': url})
            logger.error(f"html2text conversion also failed for {url}: {e_html2text}")
            return f"## Conversion Failed for {url}\n\n_Source: {url}_\n\n**Error during Pandoc conversion:**\n```\n{e_pandoc}\n```\n\n**Error during html2text fallback:**\n```\n{e_html2text}\n```\n\nContent might be malformed or too complex for automated conversion."


def save_markdown(content, url, job_output_dir, title):
    filepath = safe_filename(url, job_output_dir) 
    
    source_url_line = f"source_url: {url}"
    escaped_title = title.replace('"', '\\"') 
    title_line = f"title: \"{escaped_title}\"" 
    date_line = f"date_generated: {datetime.now(timezone.utc).isoformat()}"
    # Add a sha256 hash of the markdown content for simple versioning/change detection if needed later
    # import hashlib
    # content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
    # hash_line = f"content_sha256: {content_hash}"
    # frontmatter = f"---\n{title_line}\n{source_url_line}\n{date_line}\n{hash_line}\n---\n\n"
    frontmatter = f"---\n{title_line}\n{source_url_line}\n{date_line}\n---\n\n"

    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(frontmatter + content)
        return filepath
    except Exception as e:
        logger.error(f"Error saving markdown for {url} to {filepath}: {e}")
        return None

def save_crawled_content_list(crawled_pages_content, job_output_dir, ordered_urls_for_toc):
    all_docs_path = os.path.join(job_output_dir, ALL_DOCS_FILENAME)
    
    toc = ["# Table of Contents"]
    page_order_for_file = []
    
    for url in ordered_urls_for_toc:
        page_data = crawled_pages_content.get(url)
        if page_data:
            title = page_data['title']
            # Anchor must be unique and simple
            anchor_base = re.sub(r'[^a-z0-9_]+', '-', title.lower()).strip('-') or "page"
            # Ensure uniqueness if multiple pages have same title
            anchor_suffix = 0
            anchor = anchor_base
            while f"- [{title}](#{anchor})" in toc: # Crude check, assumes title itself is unique enough for this purpose in TOC
                anchor_suffix += 1
                anchor = f"{anchor_base}-{anchor_suffix}"

            toc.append(f"- [{title}](#{anchor})") 
            page_data['anchor'] = anchor # Store the generated anchor
            page_order_for_file.append(page_data['filename'])

    toc_md = "\n".join(toc) + "\n\n---\n\n"

    try:
        with open(all_docs_path, 'w', encoding='utf-8') as f:
            f.write(toc_md)
            for url in ordered_urls_for_toc:
                page_data = crawled_pages_content.get(url)
                if page_data:
                    title = page_data['title']
                    md_content = page_data['md']
                    anchor = page_data.get('anchor', re.sub(r'[^a-z0-9_]+', '-', title.lower()).strip('-') or "page") # Fallback anchor
                    
                    f.write(f'<a name="{anchor}"></a>\n# {title}\n\n{md_content}\n\n---\n\n')
        
        order_file_path = os.path.join(job_output_dir, DOCS_SUBDIR_NAME, ORDER_FILENAME)
        with open(order_file_path, 'w', encoding='utf-8') as f:
            for fname_in_order in page_order_for_file:
                 f.write(fname_in_order + '\n')
                 
        return all_docs_path
    except Exception as e:
        logger.error(f"Error saving combined markdown or order.txt: {e}")
        return None


def create_zip_archive_for_job(job_output_dir: str) -> Tuple[Optional[str], Optional[str]]:
    docs_full_path = os.path.join(job_output_dir, DOCS_SUBDIR_NAME)
    all_docs_full_path = os.path.join(job_output_dir, ALL_DOCS_FILENAME)

    timestamp = int(time.time())
    output_zip_basename = f"{ZIP_FILENAME_PREFIX}{timestamp}.zip"
    local_zip_path = os.path.join(job_output_dir, output_zip_basename)
    
    files_to_zip_map = {}

    if os.path.isdir(docs_full_path):
        for root, _, files in os.walk(docs_full_path):
            for fname in files:
                if fname.endswith('.md') or fname == ORDER_FILENAME:
                    real_path = os.path.join(root, fname)
                    arcname = os.path.relpath(real_path, job_output_dir)
                    files_to_zip_map[arcname] = real_path
    
    if os.path.isfile(all_docs_full_path):
        files_to_zip_map[ALL_DOCS_FILENAME] = all_docs_full_path

    if not files_to_zip_map:
        logger.warning(f"No files to zip in {job_output_dir}.")
        return None, None

    try:
        with zipfile.ZipFile(local_zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for arcname, real_path in files_to_zip_map.items():
                zipf.write(real_path, arcname)
        return local_zip_path, output_zip_basename
    except Exception as e:
        logger.error(f"Error creating ZIP file for job {os.path.basename(job_output_dir)}: {e}")
        return None, None

# --- Main Celery Task ---

@celery_app.task(bind=True, ignore_result=False)
def process_site_task(self, site_url: str, output_format: str = "zip", path_prefix: Optional[str] = None, use_regex: bool = False, custom_regex: Optional[str] = None, max_pages: int = 1000):
    job_id = self.request.id
    job_output_dir = os.path.join("outputs", str(job_id))
    docs_output_path = os.path.join(job_output_dir, DOCS_SUBDIR_NAME) 
    os.makedirs(docs_output_path, exist_ok=True)

    self.update_state(state='STARTED', meta={'job_id': job_id, 'status': 'Initializing crawler...', 'site_url': site_url, 'output_format': output_format})

    parsed_seed = urlparse(site_url)
    seed_host = parsed_seed.hostname
    if not seed_host:
        self.update_state(state=states.FAILURE, meta={'job_id': job_id, 'status': 'Invalid seed URL: Missing hostname.', 'error': 'Invalid URL'})
        raise Ignore()

    seed_scope_prefixes_list = []
    include_prefixes_list = []
    exclude_regexes_list = []

    if path_prefix:
        include_prefixes_list = [p.strip() for p in path_prefix.split(',') if p.strip()]
        if include_prefixes_list: # Ensure it's not an empty list after stripping
            first_user_prefix_path = urlparse(urljoin(site_url, include_prefixes_list[0])).path
            seed_scope_prefixes_list.append(get_path_prefix_parts(urljoin(site_url, first_user_prefix_path), SCOPE_PATH_SEGMENT_DEPTH))
        else: # path_prefix was given but resulted in empty list (e.g. just commas)
            # Fallback to default seed URL scoping
            seed_scope_prefixes_list.append(get_path_prefix_parts(site_url, SCOPE_PATH_SEGMENT_DEPTH))

    else: 
        seed_scope_prefixes_list.append(get_path_prefix_parts(site_url, SCOPE_PATH_SEGMENT_DEPTH))
        # Add parent paths if seed URL is deep
        # This helps if seed is example.com/a/b/c, to also discover links in /a/b or /a
        # Note: in_scope logic for include_prefixes takes precedence. This is for general discovery.
        temp_path_parts_list = urlparse(site_url).path.strip('/').split('/')
        if temp_path_parts_list and temp_path_parts_list[0] and len(temp_path_parts_list) > 1: # if not root and has segments
            for i in range(1, len(temp_path_parts_list)):
                # Create progressively shorter prefixes from the seed URL path
                shorter_prefix_path = "/" + "/".join(temp_path_parts_list[:i])
                # Use SCOPE_PATH_SEGMENT_DEPTH or actual depth, whichever is smaller for these parent paths
                effective_depth = min(SCOPE_PATH_SEGMENT_DEPTH, i) 
                seed_scope_prefixes_list.append(get_path_prefix_parts(urljoin(site_url, shorter_prefix_path), effective_depth))
        # Remove duplicate prefixes that might have been added
        seed_scope_prefixes_list = [list(x) for x in set(tuple(x) for x in seed_scope_prefixes_list)]


    if use_regex and custom_regex:
        exclude_regexes_list = [r.strip() for r in custom_regex.splitlines() if r.strip()] # Use splitlines() for multiline input

    # BFS queue and visited set
    # Ensure initial site_url is cleaned (no fragment)
    cleaned_site_url = urlparse(site_url)._replace(fragment="").geturl()
    queue = deque([(cleaned_site_url, 0)]) # (url, depth)
    visited_urls = {cleaned_site_url}

    crawled_pages_content = {} 
    ordered_urls_for_toc = [] 

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})
    
    current_request_delay = INITIAL_REQUEST_DELAY_SECONDS
    consecutive_html_successes = 0
    in_penalty_box_until = 0 
    penalty_box_request_count_remaining = 0

    pages_crawled_count = 0
    total_pages_fetched = 0

    self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': 'Starting crawl...', 'total_queued': len(queue), 'params': {'site_url':site_url, 'output_format': output_format, 'path_prefix':path_prefix, 'use_regex':use_regex, 'custom_regex':custom_regex, 'max_pages':max_pages}})

    while queue and (pages_crawled_count < max_pages) and (not STRICT_PAGE_LIMIT_ENFORCEMENT or total_pages_fetched < max_pages):
        if STRICT_PAGE_LIMIT_ENFORCEMENT and total_pages_fetched >= max_pages:
            self.update_state(state='PROGRESS', meta={
                'job_id': job_id, 
                'status': f'[STRICT LIMIT] Reached total page fetch limit of {max_pages}. Stopping crawl.', 
                'total_pages_fetched': total_pages_fetched
            })
            break
            
        current_url, depth = queue.popleft()
        
        if time.time() < in_penalty_box_until or penalty_box_request_count_remaining > 0:
            actual_delay = MAX_REQUEST_DELAY_SECONDS
            if penalty_box_request_count_remaining > 0:
                penalty_box_request_count_remaining -=1
        else: actual_delay = current_request_delay

        self.update_state(state='PROGRESS', meta={
            'job_id': job_id, 
            'status': f'Crawling: {current_url} ({pages_crawled_count}/{max_pages})', 
            'current_url': current_url, 
            'depth': depth, 
            'q_size': len(queue), 
            'crawled': pages_crawled_count, 
            'max_pages': max_pages,
            'delay': round(actual_delay,2)
        })

        html_content, headers, status_code, error = fetch_page_content(current_url, session, self, actual_delay)

        if status_code == 429 or status_code == 503: # Rate limit or server busy
            self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': f'Server pressure ({status_code}) on {current_url}. Applying penalty.', 'current_url': current_url})
            retry_after_header = headers.get('Retry-After')
            wait_time = RETRY_AFTER_DEFAULT_SECONDS
            if retry_after_header:
                try: wait_time = int(retry_after_header)
                except ValueError: 
                    try: 
                        # Attempt to parse HTTP date
                        retry_dt = parsedate_to_datetime(retry_after_header)
                        # Ensure it's timezone-aware for correct comparison if not already
                        if retry_dt.tzinfo is None or retry_dt.tzinfo.utcoffset(retry_dt) is None:
                           retry_dt = retry_dt.replace(tzinfo=timezone.utc) # Assume UTC if not specified
                        wait_time = (retry_dt - datetime.now(timezone.utc)).total_seconds()
                    except Exception: pass # Keep default if date parsing fails
            wait_time = max(MIN_REQUEST_DELAY_SECONDS, min(wait_time, MAX_REQUEST_DELAY_SECONDS * 3)) # Cap wait time slightly higher

            current_request_delay = min(MAX_REQUEST_DELAY_SECONDS, current_request_delay + DELAY_DECREMENT_STEP_SECONDS * 2) # Increase global delay more significantly
            in_penalty_box_until = time.time() + PENALTY_BOX_DURATION_SECONDS 
            
            logger.info(f"Rate limited on {current_url}. Waiting for {wait_time:.2f}s. New delay: {current_request_delay:.2f}s")
            time.sleep(wait_time) 
            queue.appendleft((current_url, depth)) 
            total_pages_fetched -=1 # Decrement as this fetch attempt failed before content processing
            continue 

        if error or not html_content:
            self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': f'Failed to fetch {current_url}: {error or "No content"}', 'current_url': current_url, 'error_page': True})
            consecutive_html_successes = 0 
            current_request_delay = min(MAX_REQUEST_DELAY_SECONDS, current_request_delay + DELAY_DECREMENT_STEP_SECONDS) 
            total_pages_fetched -=1 # Decrement as this fetch attempt failed before content processing (if STRICT_PAGE_LIMIT_ENFORCEMENT)
            continue
        
        total_pages_fetched += 1 # Increment successfully fetched pages

        if not is_html_url(current_url, headers):
            self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': f'Skipped (not HTML): {current_url}', 'current_url': current_url})
            consecutive_html_successes = 0 
            # This was a fetch, but not a crawl target, so don't decrement pages_crawled_count
            # but total_pages_fetched should remain incremented.
            continue
        
        # Successfully fetched an HTML page
        consecutive_html_successes += 1
        if consecutive_html_successes >= SUCCESSFUL_HTML_PAGES_TO_SHORTEN_DELAY:
            current_request_delay = max(MIN_REQUEST_DELAY_SECONDS, current_request_delay - DELAY_DECREMENT_STEP_SECONDS)
            consecutive_html_successes = 0 

        # Check strict limit again *after* confirming it's HTML and *before* processing it (which is costly)
        # This prevents processing the Nth page if N is the limit.
        if STRICT_PAGE_LIMIT_ENFORCEMENT and pages_crawled_count >= max_pages : #  pages_crawled_count for pages actually processed
             self.update_state(state='PROGRESS', meta={
                'job_id': job_id, 
                'status': f'[STRICT LIMIT] Reached processed page limit of {max_pages} before processing {current_url}. Stopping crawl.', 
                'total_pages_fetched': total_pages_fetched,
                'pages_crawled_count': pages_crawled_count
            })
             break

        pages_crawled_count += 1
        ordered_urls_for_toc.append(current_url)

        soup_title_parser = BeautifulSoup(html_content, 'html.parser')
        page_title_tag = soup_title_parser.find('title')
        page_title = page_title_tag.string.strip() if page_title_tag and page_title_tag.string else \
                     (urlparse(current_url).path.split('/')[-1] or urlparse(current_url).hostname or "Untitled Page")
        
        self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': f'Converting: {current_url} ({pages_crawled_count}/{max_pages})', 'current_url': current_url})
        
        # Use the new combined conversion function
        md_content = convert_html_to_markdown(html_content, current_url, self)
        
        filename_for_order = safe_filename(current_url, job_output_dir, for_ordering=True)
        
        saved_filepath = save_markdown(md_content, current_url, job_output_dir, page_title)
        if saved_filepath:
            crawled_pages_content[current_url] = {'md': md_content, 'title': page_title, 'filename': filename_for_order}
            self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': f'Saved: {os.path.basename(saved_filepath)} ({pages_crawled_count}/{max_pages})', 'current_url': current_url, 'pages_saved': pages_crawled_count})
        else:
            self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': f'Failed to save MD for: {current_url}', 'current_url': current_url, 'error_saving': True})

        # Discover new links only if we haven't hit page limits
        if (pages_crawled_count < max_pages) and (not STRICT_PAGE_LIMIT_ENFORCEMENT or total_pages_fetched < max_pages):
            self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': f'Extracting links from: {current_url}', 'current_url': current_url})
            nav_links = extract_nav_links(html_content, current_url, seed_host, seed_scope_prefixes_list, include_prefixes_list, exclude_regexes_list)
            for link in nav_links:
                if link not in visited_urls:
                    # Check if adding this link would exceed total_pages_fetched limit if strict
                    if STRICT_PAGE_LIMIT_ENFORCEMENT and (len(visited_urls) + (len(queue) -1) ) >= max_pages : # -1 because current is popped
                        continue # Don't add if it would push us over the fetch limit
                    visited_urls.add(link)
                    queue.append((link, depth + 1))
            self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': f'Queue updated: {len(queue)} URLs pending, {pages_crawled_count}/{max_pages} processed', 'total_queued': len(visited_urls)})


    self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': 'Crawl finished. Generating combined files...'})

    all_docs_path = None
    if crawled_pages_content:
        all_docs_path = save_crawled_content_list(crawled_pages_content, job_output_dir, ordered_urls_for_toc)
        if all_docs_path:
            self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': f'Combined document saved: {os.path.basename(all_docs_path)}', 'all_docs_generated': True})
        else:
            self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': 'Failed to save combined document.', 'all_docs_generated': False, 'error': True})


    # --- Artifact Preparation and R2 Upload ---
    file_to_upload_local_path = None
    r2_object_key_final = None
    final_artifact_basename = None 

    if output_format == "single_md":
        if all_docs_path and os.path.exists(all_docs_path):
            file_to_upload_local_path = all_docs_path
            final_artifact_basename = ALL_DOCS_FILENAME 
            r2_object_key_final = f"{job_id}/{final_artifact_basename}" # R2 key includes job_id prefix
            self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': f'Preparing {final_artifact_basename} for upload.'})
        else:
            self.update_state(state=states.FAILURE, meta={'job_id': job_id, 'status': 'Error: single_md format requested, but all_docs.md not found or not generated.', 'error': 'all_docs.md missing for single_md export'})
            raise Ignore() # Stop task execution
    elif output_format == "zip":
        self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': 'Creating ZIP archive...'})
        # create_zip_archive_for_job returns (local_path, basename)
        zip_file_local_path, zip_file_basename = create_zip_archive_for_job(job_output_dir)
        if zip_file_local_path and zip_file_basename:
            file_to_upload_local_path = zip_file_local_path
            final_artifact_basename = zip_file_basename 
            r2_object_key_final = f"{job_id}/{final_artifact_basename}" # R2 key includes job_id prefix
            self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': f'ZIP archive created: {final_artifact_basename}. Preparing for upload.'})
        else:
            self.update_state(state=states.FAILURE, meta={'job_id': job_id, 'status': 'Error: Failed to create ZIP archive.', 'error': 'ZIP creation failed'})
            raise Ignore() # Stop task execution
    else:
        self.update_state(state=states.FAILURE, meta={'job_id': job_id, 'status': f'Error: Unknown output_format requested: {output_format}', 'error': 'Invalid output_format'})
        raise Ignore() # Stop task execution

    r2_bucket_name = None # Initialize
    if file_to_upload_local_path and r2_object_key_final:
        try:
            r2_bucket_name = os.environ['R2_BUCKET_NAME']
        except KeyError:
            self.update_state(state=states.FAILURE, meta={'job_id': job_id, 'status': 'Configuration error: R2_BUCKET_NAME missing.', 'error': 'R2_BUCKET_NAME not set'})
            raise Ignore()
        
        s3_client = get_r2_client(self) # self is task_instance
        uploaded_key = upload_to_r2(s3_client, file_to_upload_local_path, r2_bucket_name, r2_object_key_final, self)
        
        if not uploaded_key: # upload_to_r2 now raises Ignore on failure
            # This path should ideally not be reached if upload_to_r2 raises Ignore.
            self.update_state(state=states.FAILURE, meta={'job_id': job_id, 'status': 'R2 Upload failed. Check logs.', 'error': 'R2 upload returned no key'})
            raise Ignore()
        self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': f'Successfully uploaded {uploaded_key} to R2 bucket {r2_bucket_name}.'})
    else:
        self.update_state(state=states.FAILURE, meta={'job_id': job_id, 'status': 'Error: No file was prepared for R2 upload.', 'error': 'File for R2 upload missing'})
        raise Ignore()


    self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': 'Cleaning up local files...'})
    try:
        if os.path.exists(job_output_dir):
            shutil.rmtree(job_output_dir)
            self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': 'Local files cleaned up.'})
    except Exception as e:
        logger.warning(f"Error during cleanup for job {job_id}: {e}. File was uploaded to R2.")
        self.update_state(state='PROGRESS', meta={'job_id': job_id, 'status': f'Warning: Error during cleanup: {e}. File was uploaded to R2.', 'cleanup_error': str(e)})


    final_status_message = f'Completed. Output: {output_format}. Artifact: {r2_object_key_final}. Pages crawled: {pages_crawled_count}. Total fetched: {total_pages_fetched}.'
    if not pages_crawled_count:
        final_status_message = f'Completed. No pages were crawled or matched criteria. Total pages fetched: {total_pages_fetched}.'
    
    if STRICT_PAGE_LIMIT_ENFORCEMENT:
         final_status_message += f" (Strict page limit: {max_pages})"


    return {
        'job_id': job_id,
        'status': final_status_message,
        'site_url': site_url,
        'pages_crawled': pages_crawled_count,
        'total_pages_fetched': total_pages_fetched,
        'r2Bucket': r2_bucket_name,
        'r2ObjectKey': r2_object_key_final, 
        'outputFormat': output_format,
        'params_used': {'path_prefix':path_prefix, 'use_regex':use_regex, 'custom_regex':custom_regex, 'max_pages':max_pages, 'output_format': output_format, 'strict_limit': STRICT_PAGE_LIMIT_ENFORCEMENT},
        'expiresAt': (datetime.now() + timedelta(hours=R2_OBJECT_EXPIRATION_HOURS)).isoformat()
    }

# --- Cleanup Task for R2 Objects ---

@celery_app.task
def cleanup_expired_r2_objects():
    """
    Scheduled task to clean up expired R2 objects.
    This will remove files that have been stored in R2 for longer than R2_OBJECT_EXPIRATION_HOURS.
    """
    logger.info("Starting cleanup of expired R2 objects")
    
    try:
        # Get R2 bucket name
        r2_bucket_name = os.environ['R2_BUCKET_NAME']
        
        # Initialize R2 client
        s3_client = boto3.client(
            service_name='s3',
            endpoint_url=f'https://{os.environ["R2_ACCOUNT_ID"]}.r2.cloudflarestorage.com',
            aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
            aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
            region_name='auto'
        )
        
        # List all objects in the bucket
        response = s3_client.list_objects_v2(Bucket=r2_bucket_name)
        
        current_time = datetime.now()
        deleted_count = 0
        
        if 'Contents' in response:
            for obj in response['Contents']:
                object_key = obj['Key']
                last_modified = obj['LastModified']
                
                # Calculate object age in hours
                object_age = (current_time.replace(tzinfo=None) - last_modified.replace(tzinfo=None)).total_seconds() / 3600
                
                # Check if the object is older than the expiration time
                if object_age >= R2_OBJECT_EXPIRATION_HOURS:
                    logger.info(f"Deleting expired object: {object_key}")
                    if delete_from_r2(s3_client, r2_bucket_name, object_key):
                        deleted_count += 1
                    
                # Alternative method: Check metadata if age calculation isn't reliable
                try:
                    obj_metadata = s3_client.head_object(Bucket=r2_bucket_name, Key=object_key)
                    if 'Metadata' in obj_metadata and 'expiration_time' in obj_metadata['Metadata']:
                        expiration_time = datetime.fromisoformat(obj_metadata['Metadata']['expiration_time'])
                        if current_time >= expiration_time:
                            logger.info(f"Deleting expired object based on metadata: {object_key}")
                            if delete_from_r2(s3_client, r2_bucket_name, object_key) and object_key not in [obj['Key'] for obj in response['Contents'] if obj['Key'] == object_key]:
                                deleted_count += 1
                except Exception as e:
                    logger.error(f"Error checking metadata for {object_key}: {e}")
        
        logger.info(f"Completed cleanup: {deleted_count} expired objects deleted")
        return {"deleted_count": deleted_count}
        
    except Exception as e:
        logger.error(f"Error during R2 cleanup task: {e}")
        return {"error": str(e)}

# Configure scheduled tasks
@celery_app.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    # Run the cleanup task every hour
    sender.add_periodic_task(3600.0, cleanup_expired_r2_objects.s(), name='cleanup-expired-r2-objects-every-hour')
    
    # For testing, you can also run it more frequently
    # sender.add_periodic_task(60.0, cleanup_expired_r2_objects.s(), name='cleanup-expired-r2-objects-every-minute')