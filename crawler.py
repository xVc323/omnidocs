import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from urllib.parse import urljoin, urlparse
import tldextract 
import pypandoc
import os
from datetime import datetime, timezone
import re
import html2text
from collections import deque
import mimetypes
import time
from email.utils import parsedate_to_datetime
import argparse

# === Toggle this variable to strictly enforce page limits across both phases ===
STRICT_PAGE_LIMIT_ENFORCEMENT = True  # Set to False to revert to original behavior
# ===========================================================================

"""
OmniDocs Crawler

Usage:
    python crawler.py <seed_url> [--max-pages N] [--include-prefix PREFIX] [--exclude-regex REGEX]

Options:
    seed_url            The starting URL for the crawl (required)
    --max-pages N       Maximum number of pages to crawl (default: 10)
    --include-prefix    Path prefix to include (can be used multiple times)
    --exclude-regex     Regex to exclude URLs (can be used multiple times)

Examples:
    # Basic crawl
    python crawler.py https://docs.example.com

    # Only include URLs starting with /api or /guide
    python crawler.py https://docs.example.com --include-prefix /api --include-prefix /guide

    # Exclude URLs matching a pattern
    python crawler.py https://docs.example.com --exclude-regex '.*logout.*' --exclude-regex '/private/'

    # Combine both
    python crawler.py https://docs.example.com --include-prefix /api --exclude-regex '.*beta.*'

Description:
    The crawler will only include URLs that match any of the given --include-prefix values (if provided),
    and will exclude any URLs that match any of the given --exclude-regex patterns. If neither is provided,
    the default scoping rules apply (same host and path prefix as the seed URL).
"""

# --- Adaptive Delay Parameters ---
INITIAL_REQUEST_DELAY_SECONDS = 0.75    # Start with this delay
MIN_REQUEST_DELAY_SECONDS = 0.25        # Crawl no faster than this
MAX_REQUEST_DELAY_SECONDS = 5.0         # If rate limited, general delay becomes this
RETRY_AFTER_DEFAULT_SECONDS = 8.0       # Default for 429 if no Retry-After header
SUCCESSFUL_HTML_PAGES_TO_SHORTEN_DELAY = 5 # Consecutively saved HTML pages to trigger delay reduction
DELAY_DECREMENT_STEP_SECONDS = 0.25     # How much to reduce general delay by

# --- Penalty Box Parameters ---
PENALTY_BOX_DURATION_SECONDS = 20.0     # How long to stay in penalty box (using MAX_REQUEST_DELAY)
PENALTY_BOX_REQUEST_COUNT = 5           # Alternative: Number of requests to make at MAX_REQUEST_DELAY

USER_AGENT = "OmniDocsCrawler/0.1 (respectful-crawler; https://github.com/xvc323/omnidocs)"
SCOPE_PATH_SEGMENT_DEPTH = 2
MAIN_CONTENT_SELECTORS = [
    'div.theme-doc-markdown.markdown', 
    'article.md-content__inner',       
    'div[role="main"]',                
    'main',                            
    'body'                             
]

def safe_filename(url):
    parsed = urlparse(url)
    path = parsed.path.strip('/').replace('/', '_')
    if not path: path = 'index'
    return path + '.md'

def html_tables_to_md(md):
    def table_replacer(match):
        table_html = match.group(0)
        h = html2text.HTML2Text()
        h.body_width = 0; h.ignore_links = False; h.ignore_images = True 
        h.ignore_emphasis = False; h.bypass_tables = False
        try: return h.handle(table_html)
        except Exception: return f"[html2text failed to convert table]"
    return re.sub(r'<table[\s\S]*?</table>', table_replacer, md, flags=re.IGNORECASE)

def clean_markdown_artifacts(md):
    md = re.sub(r'^:+.*$', '', md, flags=re.MULTILINE) 
    md = re.sub(r'\{\.[^\}]*\}', '', md) 
    md = re.sub(r'^-+\|[-|]+$', '', md, flags=re.MULTILINE) 
    return md

def final_html_strip_and_prettify(md_text):
    # Initial cleanup of specific problematic characters
    md_text = md_text.replace('\u200B', '') # Zero Width Space
    md_text = md_text.replace('â€‹', '')    # Mojibake for ZWS

    soup = BeautifulSoup(md_text, 'html.parser')

    # Enhanced <img> tag handling
    for img in soup.find_all('img'):
        alt = img.get('alt', '').strip()
        src = img.get('src', '')

        # Default action is to remove the image entirely
        replacement_node = None 

        if src and not src.startswith('data:image'): # Regular image URL (not base64)
            # Keep as Markdown image link if alt text exists, or if src seems meaningful
            if alt:
                placeholder = f"![{alt}]({src})"
            elif not any(kw in src.lower() for kw in ['spacer', 'pixel', 'icon']) and len(src) > 10: # Heuristic for meaningful src
                placeholder = f"![Image]({src})" # Placeholder alt
            else: # Likely decorative or icon with no alt, remove by not setting placeholder
                pass 
            
            if placeholder:
                replacement_node = BeautifulSoup(placeholder, 'html.parser').contents[0]

        elif alt: # Base64 image OR no src, but has alt text
            # For base64 or no-src images, if alt text exists, represent it as text.
            placeholder = f"[Image: {alt}]"
            replacement_node = NavigableString(placeholder)
        
        # else: base64 image with no alt, or no src and no alt. Will be removed.

        parent = img.parent
        # Check if image is the sole significant content of a link
        is_sole_link_content = False
        if parent and parent.name == 'a':
            significant_children = [
                child for child in parent.contents 
                if child != img and (isinstance(child, Tag) or (isinstance(child, NavigableString) and child.strip()))
            ]
            if not significant_children:
                is_sole_link_content = True

        if is_sole_link_content:
            link_tag = parent
            link_href = link_tag.get('href')
            # Try to get link text from aria-label, then alt, then placeholder text, then "link"
            link_text_content = ""
            if replacement_node and isinstance(replacement_node, NavigableString):
                link_text_content = str(replacement_node)
            elif replacement_node and replacement_node.name == 'img' and replacement_node.get('alt'): # if it was ![alt](src)
                 link_text_content = replacement_node.get('alt')
            
            link_text_from_attrs = link_tag.get('aria-label', alt if alt else link_text_content).strip()
            
            final_link_text = link_text_from_attrs if link_text_from_attrs else "link"

            if link_href and not link_href.startswith('data:'):
                new_link_md = f"[{final_link_text}]({link_href})"
                link_tag.replace_with(BeautifulSoup(new_link_md, 'html.parser').contents[0])
            elif final_link_text != "link" or (final_link_text == "link" and link_text_content): # if we have some text
                link_tag.replace_with(NavigableString(final_link_text))
            else: # No good text, no good href
                link_tag.decompose()
        elif replacement_node:
            img.replace_with(replacement_node)
        else: # No replacement, remove the image
            img.decompose()
            
    # Unwrap common layout/styling tags
    tags_to_unwrap = ['div', 'span', 'font', 'section', 'article', 'aside', 'header', 'footer', 'main', 'figure', 'figcaption'] 
    for tag_name in tags_to_unwrap:
        for t in soup.find_all(tag_name):
            for child_text_node in t.find_all(string=True): # Clean ZWS inside before unwrapping
                child_text_node.replace_with(child_text_node.replace('\u200B', '').replace('â€‹', ''))
            t.unwrap()

    # Remove empty <a> tags that might result from ZWS cleaning or other operations
    for a_tag in soup.find_all('a'):
        # Check if text content is empty after stripping whitespace and known artifacts
        tag_text = a_tag.get_text(strip=True).replace('\u200B', '').replace('â€‹', '')
        if not tag_text and not a_tag.find_all(True, recursive=False): # No text and no child elements
            a_tag.decompose()

    cleaned_md_string = str(soup)
    
    # Aggressively remove any remaining Markdown image tags for base64 data
    # Regex: ![](data:image/[^;]+;base64,[^)]+)
    cleaned_md_string = re.sub(r'!\[[^\]]*\]\(data:image/[^;]+;base64,[^)]+\)', '[Embedded Image Removed]', cleaned_md_string)
    
    # Remove excessive newlines
    cleaned_md_string = re.sub(r'\n{3,}', '\n\n', cleaned_md_string)
    cleaned_md_string = re.sub(r'^\s*\n', '', cleaned_md_string, flags=re.MULTILINE) 
    
    # Final cleanup of known artifacts
    cleaned_md_string = cleaned_md_string.replace('\u200B', '').replace('â€‹', '')
    # Consolidate repeated placeholders like "[Embedded Image][Embedded Image]"
    cleaned_md_string = re.sub(r'(\[Embedded Image(?: Removed)?\]\s*){2,}', r'\1', cleaned_md_string)


    return cleaned_md_string.strip() + '\n'

def in_scope(url, seed_host, seed_scope_prefix_parts_list, include_prefixes=None, exclude_regexes=None):
    parsed = urlparse(url)
    if parsed.hostname != seed_host:
        return False
    current_path = parsed.path
    # Manual include prefix override
    if include_prefixes:
        if not any(current_path.startswith(prefix) for prefix in include_prefixes):
            return False
    # Manual exclude regex override
    if exclude_regexes:
        for regex in exclude_regexes:
            if re.search(regex, url):
                return False
    current_path_parts = [p for p in parsed.path.split('/') if p]
    if len(current_path_parts) < len(seed_scope_prefix_parts_list):
        return False
    for i, prefix_part in enumerate(seed_scope_prefix_parts_list):
        if current_path_parts[i] != prefix_part:
            return False
    return True

def is_html_url(url):
    parsed = urlparse(url)
    path = parsed.path.lower()
    if '#' in path: path = path.split('#', 1)[0]
    if '?' in path: path = path.split('?', 1)[0]
    basename = os.path.basename(path)
    if path.endswith(('/', '.html', '.htm')) or (basename and '.' not in basename): return True
    non_html_exts = {
        '.zip', '.pdf', '.tar', '.bz2', '.gz', '.rar', '.7z', '.png', '.jpg', '.jpeg', 
        '.gif', '.svg', '.webp', '.bmp', '.ico', '.mp3', '.mp4', '.avi', '.mov', '.ogg', 
        '.webm', '.exe', '.msi', '.dmg', '.deb', '.rpm', '.whl', '.txt', '.csv', '.json', 
        '.xml', '.yaml', '.yml', '.css', '.js', '.doc', '.docx', '.xls', '.xlsx', 
        '.ppt', '.pptx', '.odt'
    }
    ext = os.path.splitext(basename)[1]
    return ext not in non_html_exts

def crawl_docs(seed_url, max_pages=10, include_prefixes=None, exclude_regexes=None):
    try:
        pandoc_ver = pypandoc.get_pandoc_version()
        print(f"Pandoc version: {pandoc_ver} found.")
    except OSError: 
        print("Pandoc is not installed or not found in PATH. Please install: https://pandoc.org/installing.html")
        return

    print(f"Starting crawl from: {seed_url} with adaptive delay and content extraction.")
    os.makedirs('docs', exist_ok=True)
    
    seed_parsed = urlparse(seed_url)
    seed_host = seed_parsed.hostname
    initial_path_segments = [p for p in seed_parsed.path.split('/') if p]
    seed_scope_prefix_parts = initial_path_segments[:SCOPE_PATH_SEGMENT_DEPTH] if SCOPE_PATH_SEGMENT_DEPTH > 0 else []
    print(f"Scope: Host='{seed_host}', Path Prefix Segments={seed_scope_prefix_parts}")

    headers = {'User-Agent': USER_AGENT}
    
    delay_state = {
        "current_delay_seconds": INITIAL_REQUEST_DELAY_SECONDS,
        "consecutive_success_count": 0,
        "in_penalty_box_until_time": 0, # Timestamp until which we are in penalty box
        "penalty_box_request_counter": 0 # Alternative counter for penalty box
    }

    # Total page counter for strict enforcement
    total_pages_fetched = 0

    def handle_rate_limit(
            url_that_was_limited,
            response_headers,
            current_queue: deque | None = None,
            is_graph_pass: bool = True,
        ):
        """
        Handles HTTP 429 responses (rate‑limit).
        If `current_queue` is provided (link‑graph pass) the URL is re‑queued so it
        will be retried; when `current_queue` is None (save‑pass) the caller skips
        the URL for now.
        The routine also updates the global adaptive‑delay state and sleeps for the
        Retry‑After duration (or a sensible default) before returning.
        """
        delay_state["consecutive_success_count"] = 0
        # Enter penalty box: subsequent requests will use MAX_REQUEST_DELAY_SECONDS
        delay_state["in_penalty_box_until_time"] = time.time() + PENALTY_BOX_DURATION_SECONDS
        delay_state["penalty_box_request_counter"] = PENALTY_BOX_REQUEST_COUNT # Reset counter
        delay_state["current_delay_seconds"] = MAX_REQUEST_DELAY_SECONDS # Immediately slow down general pace
        log_prefix = "[GRAPH]" if is_graph_pass else "[SAVE]"
        print(f"{log_prefix} Rate limited (429). General delay set to {delay_state['current_delay_seconds']:.2f}s. Entering penalty box.")
        specific_wait = RETRY_AFTER_DEFAULT_SECONDS
        r_after = response_headers.get("Retry-After")
        if r_after:
            try:
                specific_wait = float(int(r_after))
            except ValueError:
                try:
                    r_date = parsedate_to_datetime(r_after)
                    r_date_utc = r_date.astimezone(timezone.utc) if r_date.tzinfo else r_date.replace(tzinfo=timezone.utc)
                    specific_wait = max(0.1, (r_date_utc - datetime.now(timezone.utc)).total_seconds())
                except Exception:
                    pass
        print(f"{log_prefix} Specific wait for 429 on {url_that_was_limited}: {specific_wait:.1f}s. Re-queueing.")
        time.sleep(specific_wait)
        if current_queue is not None:
            # Only re‑queue during the graph pass; the save pass hands in None.
            if url_that_was_limited not in current_queue:
                current_queue.appendleft(url_that_was_limited)
            return True   # Re‑queued
        return False      # Nothing re‑queued

    def adapt_delay_after_success(is_graph_pass=True):
        """Adapts delay if not in penalty box."""
        log_prefix = "[GRAPH]" if is_graph_pass else "[SAVE]"
        
        # Check if penalty box duration/count has passed
        if delay_state["in_penalty_box_until_time"] > time.time():
        # if delay_state["penalty_box_request_counter"] > 0: # Using request counter
            # delay_state["penalty_box_request_counter"] -=1
            # print(f"{log_prefix} In penalty box (delay: {delay_state['current_delay_seconds']:.2f}s). {delay_state['penalty_box_request_counter']} requests left in box.")
            # No speed up while in penalty box, keep MAX_REQUEST_DELAY_SECONDS
            delay_state["current_delay_seconds"] = MAX_REQUEST_DELAY_SECONDS # Ensure it stays max
            delay_state["consecutive_success_count"] = 0 # Don't count successes in penalty box towards speedup
            return

        # If penalty box time has passed, reset to initial and allow speedup
        if delay_state["current_delay_seconds"] == MAX_REQUEST_DELAY_SECONDS and delay_state["in_penalty_box_until_time"] <= time.time():
            print(f"{log_prefix} Exited penalty box. Resetting delay to initial.")
            delay_state["current_delay_seconds"] = INITIAL_REQUEST_DELAY_SECONDS
            delay_state["consecutive_success_count"] = 0 # Fresh start for speedup

        delay_state["consecutive_success_count"] += 1
        if delay_state["consecutive_success_count"] >= SUCCESSFUL_HTML_PAGES_TO_SHORTEN_DELAY:
            new_delay = max(MIN_REQUEST_DELAY_SECONDS, delay_state["current_delay_seconds"] - DELAY_DECREMENT_STEP_SECONDS)
            if new_delay < delay_state["current_delay_seconds"]:
                print(f"{log_prefix} Sustained success. Reducing delay to {new_delay:.2f}s.")
                delay_state["current_delay_seconds"] = new_delay
            delay_state["consecutive_success_count"] = 0

    # --- First Pass: Building Link Graph ---
    print("\n--- First Pass: Building Link Graph ---")
    visited_for_graph = set()
    queue_for_graph = deque([seed_url])
    link_graph = {}
    pages_discovered_graph = 0
    discovery_order = []  # Track discovery order
    nav_order = []        # Will hold nav order if found
    nav_order_found = False

    # Try to extract nav order from the seed page
    try:
        resp_nav = requests.get(seed_url, timeout=20, headers=headers)
        if resp_nav.status_code == 200 and 'text/html' in resp_nav.headers.get('Content-Type', '').lower():
            soup_nav = BeautifulSoup(resp_nav.text, "html.parser")
            nav_candidates = soup_nav.select('nav, .sidebar, .toc, #sidebar, #nav, #toc')
            nav_links = []
            for nav in nav_candidates:
                for a_tag in nav.find_all('a', href=True):
                    href = a_tag['href'].strip()
                    abs_url = urljoin(seed_url, href)
                    if not urlparse(abs_url).scheme or not urlparse(abs_url).netloc:
                        continue
                    if in_scope(abs_url, seed_host, seed_scope_prefix_parts, include_prefixes, exclude_regexes) and is_html_url(abs_url):
                        if abs_url not in nav_links:
                            nav_links.append(abs_url)
            if nav_links:
                nav_order = nav_links
                nav_order_found = True
                print(f"[NAV] Extracted {len(nav_order)} links from nav/sidebar/toc.")
    except Exception as e:
        print(f"[NAV-ERROR] Could not extract nav order: {e}")

    while queue_for_graph and pages_discovered_graph < max_pages:
        url = queue_for_graph.popleft()
        if url in visited_for_graph or not is_html_url(url):
            visited_for_graph.add(url); continue
        if url not in discovery_order:
            discovery_order.append(url)

        # Apply current delay, which might be MAX if in penalty box
        current_effective_delay = delay_state["current_delay_seconds"]
        # if delay_state["in_penalty_box_until_time"] > time.time(): # Check again before sleep
        #     current_effective_delay = MAX_REQUEST_DELAY_SECONDS
            
        print(f"Adaptive delay (graph): {current_effective_delay:.2f}s...")
        time.sleep(current_effective_delay)
        
        outlinks = set()
        try:
            print(f"[GRAPH] Fetching: {url}")
            resp = requests.get(url, timeout=20, headers=headers)

            if resp.status_code == 429:
                handle_rate_limit(url, resp.headers, queue_for_graph, is_graph_pass=True)
                continue 
            resp.raise_for_status()

            content_type = resp.headers.get('Content-Type', '').lower()
            if 'text/html' not in content_type:
                visited_for_graph.add(url); continue
            
            html_content = resp.text
            link_extraction_soup = BeautifulSoup(html_content, "html.parser") 
            for a_tag in link_extraction_soup.find_all('a', href=True):
                href = a_tag['href'].strip()
                if not href or href.startswith(('#', 'mailto:', 'tel:', 'javascript:')): continue
                abs_url = urljoin(url, href)
                if not urlparse(abs_url).scheme or not urlparse(abs_url).netloc: continue
                if in_scope(abs_url, seed_host, seed_scope_prefix_parts, include_prefixes, exclude_regexes) and is_html_url(abs_url):
                    outlinks.add(abs_url)
                    if abs_url not in visited_for_graph and abs_url not in queue_for_graph:
                        queue_for_graph.append(abs_url)
            
            link_graph[url] = outlinks
            pages_discovered_graph += 1
            # Track total pages for strict enforcement
            total_pages_fetched += 1
            print(f"[GRAPH] Discovered page {pages_discovered_graph}/{max_pages}: {url}")
            
            # Check strict limit enforcement
            if STRICT_PAGE_LIMIT_ENFORCEMENT and total_pages_fetched >= max_pages:
                print(f"[STRICT LIMIT] Reached total page limit of {max_pages}. Stopping discovery phase.")
                break
                
            adapt_delay_after_success(is_graph_pass=True)

        except Exception as e: 
            print(f"[GRAPH-ERROR] for {url}: {e}")
            delay_state["consecutive_success_count"] = 0 
            if delay_state["in_penalty_box_until_time"] <= time.time(): # Only reset if not in penalty box
                 delay_state["current_delay_seconds"] = INITIAL_REQUEST_DELAY_SECONDS
        finally:
            visited_for_graph.add(url)

    print(f"First pass complete. {len(link_graph)} mapped. {pages_discovered_graph} fetched.")
    if not link_graph or seed_url not in link_graph: print("Graph empty or seed not in graph. Exiting."); return

    # Save order file: nav order if found, else discovery order
    order_to_save = nav_order if nav_order_found else discovery_order
    try:
        with open(os.path.join('docs', 'order.txt'), 'w', encoding='utf-8') as f:
            for url in order_to_save:
                f.write(url + '\n')
        print(f"[ORDER] Saved {'nav' if nav_order_found else 'discovery'} order to docs/order.txt ({len(order_to_save)} URLs)")
    except Exception as e:
        print(f"[ORDER-ERROR] Could not save order.txt: {e}")

    def find_reachable_nodes(graph, start_node):
        if start_node not in graph: return set()
        reachable = set(); queue = deque([start_node]); processed_in_bfs = set()
        while queue:
            node = queue.popleft()
            if node in reachable: continue
            reachable.add(node); processed_in_bfs.add(node)
            for neighbor in graph.get(node, []):
                if neighbor not in processed_in_bfs and neighbor in graph: queue.append(neighbor)
        return reachable

    main_component_nodes = find_reachable_nodes(link_graph, seed_url)
    print(f"Main component size: {len(main_component_nodes)}")
    if not main_component_nodes: print("No pages reachable from seed. Exiting."); return

    # --- Second Pass: Downloading and Processing ---
    print("\n--- Second Pass: Downloading and Processing Pages in Main Component ---")
    delay_state["current_delay_seconds"] = INITIAL_REQUEST_DELAY_SECONDS # Reset for save pass
    delay_state["consecutive_success_count"] = 0
    delay_state["in_penalty_box_until_time"] = 0 # Reset penalty box for save pass
    delay_state["penalty_box_request_counter"] = 0
    
    processed_for_save = set()
    pages_saved_count = 0
    urls_to_process_ordered = sorted(list(main_component_nodes))

    # Determine remaining page limit for strict enforcement
    remaining_pages = max_pages
    if STRICT_PAGE_LIMIT_ENFORCEMENT:
        remaining_pages = max(0, max_pages - total_pages_fetched)
        print(f"[STRICT LIMIT] {total_pages_fetched} pages fetched during discovery. Remaining limit: {remaining_pages}")
        # If no pages remaining under strict limit, skip the saving phase
        if remaining_pages <= 0:
            print("[STRICT LIMIT] Maximum pages already fetched. Skipping save phase.")
            urls_to_process_ordered = []

    for url in urls_to_process_ordered:
        # Check if we've hit the limit (original or strict)
        if STRICT_PAGE_LIMIT_ENFORCEMENT and pages_saved_count >= remaining_pages:
            print(f"[STRICT LIMIT] Reached remaining page limit of {remaining_pages}. Stopping save phase.")
            break
        elif pages_saved_count >= max_pages:
            print(f"Reached maximum pages to save: {max_pages}")
            break
            
        if url in processed_for_save: continue

        current_effective_delay = delay_state["current_delay_seconds"]
        # if delay_state["in_penalty_box_until_time"] > time.time(): # Check again before sleep
        #    current_effective_delay = MAX_REQUEST_DELAY_SECONDS

        print(f"Adaptive delay (save): {current_effective_delay:.2f}s...")
        time.sleep(current_effective_delay)
        try:
            print(f"[SAVE] Fetching: {url}")
            resp = requests.get(url, timeout=20, headers=headers)

            if resp.status_code == 429:
                handle_rate_limit(url, resp.headers, None, is_graph_pass=False)  # Pass None – skip re‑queue in save pass
                # For saving pass, if 429, we skip this URL for now. 
                # It won't be re-queued in this simple loop. A more robust job system would handle this.
                processed_for_save.add(url) # Mark as processed to avoid issues if it somehow got back
                continue
            resp.raise_for_status()

            content_type = resp.headers.get('Content-Type', '').lower()
            if 'text/html' not in content_type:
                processed_for_save.add(url); continue
            
            html_content = resp.text
            main_content_soup = BeautifulSoup(html_content, 'html.parser')
            extracted_html_for_pandoc = None; used_selector = "full body (fallback)"
            for selector in MAIN_CONTENT_SELECTORS:
                target_element = main_content_soup.select_one(selector)
                if target_element: extracted_html_for_pandoc = str(target_element); used_selector = selector; break
            if not extracted_html_for_pandoc: extracted_html_for_pandoc = html_content
            
            try:
                markdown = pypandoc.convert_text(extracted_html_for_pandoc, 'gfm', format='html', extra_args=['--wrap=preserve'])
            except pypandoc.PandocError as pandoc_convert_err: 
                 print(f"Pandoc conversion error for {url} (selector: {used_selector}): {pandoc_convert_err}")
                 processed_for_save.add(url); continue 
            
            markdown = html_tables_to_md(markdown)
            markdown = clean_markdown_artifacts(markdown)
            markdown = final_html_strip_and_prettify(markdown)
            
            soup_for_title = BeautifulSoup(html_content, "html.parser") 
            title = soup_for_title.title.string.strip() if soup_for_title.title and soup_for_title.title.string else 'Untitled'
            frontmatter = f"""---\ntitle: \"{title.replace('"', "'" )}\"\nsource_url: \"{url}\"\ndate: \"{datetime.utcnow().isoformat()}Z\"\n---\n\n"""
            filename = safe_filename(url)
            with open(os.path.join('docs', filename), 'w', encoding='utf-8') as f: f.write(frontmatter); f.write(markdown)
            
            print(f"[{pages_saved_count+1}/{len(urls_to_process_ordered)}, max_pages={max_pages}] Saved: docs/{filename} (Title: {title})")
            pages_saved_count += 1
            
            # Update total pages fetched for strict limit
            if STRICT_PAGE_LIMIT_ENFORCEMENT:
                total_pages_fetched += 1
                if total_pages_fetched >= max_pages:
                    print(f"[STRICT LIMIT] Reached total page limit of {max_pages}. Will stop after processing current page.")
            
            adapt_delay_after_success(is_graph_pass=False)
        except Exception as e: 
            print(f"[SAVE-ERROR] for {url}: {e}")
            delay_state["consecutive_success_count"] = 0 
            if delay_state["in_penalty_box_until_time"] <= time.time(): # Only reset if not in penalty box
                delay_state["current_delay_seconds"] = INITIAL_REQUEST_DELAY_SECONDS
        finally:
            processed_for_save.add(url)

    # --- Final Summary ---
    if STRICT_PAGE_LIMIT_ENFORCEMENT:
        print(f"[STRICT LIMIT] Total pages fetched across both phases: {total_pages_fetched} (limit: {max_pages})")
    
    if pages_saved_count >= max_pages and pages_saved_count < len(main_component_nodes):
         print(f"Saving stopped: Reached max_pages limit ({max_pages}) for saved files.")
    elif pages_saved_count < len(main_component_nodes):
        print(f"Saving finished. Processed {pages_saved_count} of {len(main_component_nodes)} component nodes.")
    else:
        print(f"Saving complete. {pages_saved_count} pages saved.")
    print(f"Total unique URLs visited for graph: {len(visited_for_graph)}")
    print(f"Total unique URLs processed for saving from component: {len(processed_for_save)}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='OmniDocs Crawler')
    parser.add_argument('seed_url', help='Seed URL to start crawling from')
    parser.add_argument('--max-pages', type=int, default=10, help='Maximum number of pages to crawl')
    parser.add_argument('--include-prefix', action='append', help='Path prefix to include (can be used multiple times)')
    parser.add_argument('--exclude-regex', action='append', help='Regex to exclude URLs (can be used multiple times)')
    args = parser.parse_args()
    crawl_docs(args.seed_url, max_pages=args.max_pages, include_prefixes=args.include_prefix, exclude_regexes=args.exclude_regex)