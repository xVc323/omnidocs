import os
import re
import unicodedata
from urllib.parse import urlparse

def clean_heading_text_for_display(text):
    """
    Cleans text intended for display in the TOC.
    Focuses on removing rendering artifacts and mojibake,
    while preserving most technical characters.
    """
    # Remove specific mojibake patterns (e.g., from misencoded emojis)
    # This pattern for "ðŸ..." is heuristic.
    text = re.sub(r'ðŸ[^\s\w]{1,3}\S?', '', text)
    
    # Remove Unicode variation selectors if they appear as separate characters
    text = text.replace('\uFE0F', '')  # Variation Selector-16 (often for emoji style)
    text = text.replace('\u200B', '')  # Zero Width Space

    # For display, we want to keep most characters as they are in the heading.
    # This function will now be less aggressive.
    # If further cleaning is needed for specific artifacts, add targeted replaces here.
    
    cleaned_text = text.strip()

    if not cleaned_text: # If text becomes empty after stripping artifacts/whitespace
        return "Untitled Section"
        
    return cleaned_text

def generate_slug(text_for_slug, existing_slugs_for_file):
    """
    Generates a GitHub-style slug for a heading, ensuring uniqueness
    within the context of a single file's headings.
    Preserves underscores, and handles other technical characters better.
    """
    s = str(text_for_slug) # Ensure it's a string

    # Normalize (e.g., NFD for decomposing accented characters, then remove diacritics)
    # This helps with searchability and consistency if accents are not desired in slugs.
    s = unicodedata.normalize('NFD', s)
    s = "".join(c for c in s if unicodedata.category(c) != 'Mn') # Remove diacritics

    s = s.lower()
    
    # Replace characters that are problematic in URLs or not typically part of slugs,
    # but preserve letters, numbers, hyphens, and underscores.
    # Allow: a-z, 0-9, -, _
    # Convert spaces and sequences of non-allowed chars to single hyphens.
    s = re.sub(r'[^\w\s-]', '', s)  # Remove characters not alphanumeric, whitespace, hyphen, or underscore
    s = re.sub(r'\s+', '-', s)     # Replace spaces (and other whitespace) with single hyphens
    s = re.sub(r'-+', '-', s)      # Replace multiple hyphens with single hyphens
    s = s.strip('-')               # Remove leading/trailing hyphens

    if not s: 
        s = 'section' 

    original_slug = s
    counter = 1
    while s in existing_slugs_for_file:
        s = f"{original_slug}-{counter}"
        counter += 1
    return s

def extract_headings(md_content, file_anchor_base):
    headings = []
    slugs_in_current_file = set() 

    for line in md_content.splitlines():
        match = re.match(r'^(#{1,2})\s+(.+)', line) 
        if match:
            level = len(match.group(1))
            raw_heading_text = match.group(2).strip()
            
            text_for_display = clean_heading_text_for_display(raw_heading_text)
            
            if not raw_heading_text.strip() and text_for_display == "Untitled Section":
                continue

            current_heading_slug = generate_slug(raw_heading_text, slugs_in_current_file)
            slugs_in_current_file.add(current_heading_slug)
            
            full_anchor = f'#{file_anchor_base}-{current_heading_slug}'
            
            headings.append((level, text_for_display, full_anchor))
    return headings

def main():
    docs_dir = 'docs'
    if not os.path.isdir(docs_dir):
        print(f"Error: Documentation directory '{docs_dir}' not found.")
        return
    
    order_file = os.path.join(docs_dir, 'order.txt')
    ordered_files = []
    if os.path.isfile(order_file):
        # Use order.txt for ordering
        def safe_filename(url):
            parsed = urlparse(url)
            path = parsed.path.strip('/').replace('/', '_')
            if not path: path = 'index'
            return path + '.md'
        with open(order_file, 'r', encoding='utf-8') as f:
            ordered_urls = [line.strip() for line in f if line.strip()]
        for url in ordered_urls:
            fname = safe_filename(url)
            fpath = os.path.join(docs_dir, fname)
            if os.path.isfile(fpath):
                ordered_files.append(fname)
        # Add any .md files not already included (e.g., orphaned pages)
        all_md_files = [f for f in os.listdir(docs_dir) if f.endswith('.md') and os.path.isfile(os.path.join(docs_dir, f))]
        for f in all_md_files:
            if f not in ordered_files:
                ordered_files.append(f)
    else:
        # Fallback: alphabetical
        ordered_files = sorted([
            f for f in os.listdir(docs_dir) 
            if f.endswith('.md') and os.path.isfile(os.path.join(docs_dir, f))
        ])
    
    if not ordered_files:
        print(f"No .md files found in '{docs_dir}'.")
        return

    all_stitched_content_parts = []
    toc_entries = []

    for filename_md in ordered_files:
        file_path = os.path.join(docs_dir, filename_md)
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                md_content_from_file = f.read()
        except Exception as e:
            print(f"Error reading file '{filename_md}': {e}")
            continue

        file_anchor_base = os.path.splitext(filename_md)[0].lower()
        # Slugify the filename base to ensure it's URL-friendly for the anchor part
        # Use a simplified version of generate_slug for the file base (no uniqueness check needed here)
        temp_file_base = unicodedata.normalize('NFD', file_anchor_base)
        temp_file_base = "".join(c for c in temp_file_base if unicodedata.category(c) != 'Mn')
        temp_file_base = re.sub(r'[^\w\s-]', '', temp_file_base) 
        temp_file_base = re.sub(r'\s+', '-', temp_file_base)
        file_anchor_base = re.sub(r'-+', '-', temp_file_base).strip('-')
        if not file_anchor_base: 
            file_anchor_base = f"file-{ordered_files.index(filename_md)}"
        
        content_after_frontmatter = re.sub(r'^---\s*[\s\S]*?---\s*', '', md_content_from_file, count=1)
        
        current_file_headings = extract_headings(content_after_frontmatter, file_anchor_base)
        toc_entries.extend(current_file_headings)
        
        all_stitched_content_parts.append(f'\n\n<!-- Source File: {filename_md} -->\n\n')
        all_stitched_content_parts.append(content_after_frontmatter.strip())

    if not toc_entries and not all_stitched_content_parts:
        print("No content was processed. Output file will not be created.")
        return

    toc_md_lines = ["# Table of Contents\n"]
    for level, display_text, anchor_link in toc_entries:
        indent = '  ' * (level - 1) 
        toc_md_lines.append(f'{indent}- [{display_text}]({anchor_link})')
    
    final_toc_md = "\n".join(toc_md_lines)
    final_stitched_content_md = "\n\n".join(all_stitched_content_parts)

    output_filename = 'all_docs.md'
    try:
        with open(output_filename, 'w', encoding='utf-8') as f:
            f.write(final_toc_md)
            f.write('\n\n') 
            f.write(final_stitched_content_md)
        print(f"Successfully created '{output_filename}' with TOC.")
    except Exception as e:
        print(f"Error writing to '{output_filename}': {e}")

if __name__ == '__main__':
    main()