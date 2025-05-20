# OmniDocs User Guide

Welcome to OmniDocs! This guide will help you get the most out of this powerful documentation conversion tool.

## Table of Contents

- [Introduction](#introduction)
- [Getting Started](#getting-started)
- [Basic Usage](#basic-usage)
- [Advanced Options](#advanced-options)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)

## Introduction

OmniDocs is designed to make documentation archiving and offline reading simple. Whether you need to save technical documentation for offline use, create a searchable archive, or migrate content between systems, OmniDocs can help by converting web-based documentation into clean, well-formatted Markdown.

## Getting Started

### System Requirements

OmniDocs works in any modern web browser. For the best experience, we recommend:
- Chrome, Firefox, Safari, or Edge (latest versions)
- A stable internet connection
- JavaScript enabled

### Accessing OmniDocs

Visit [https://omnidocs.example.com](https://omnidocs.example.com) to access the OmniDocs web interface.

## Basic Usage

Converting a documentation site to Markdown is straightforward:

1. **Enter URL**: On the home page, enter the URL of the documentation site you want to convert.
   - Example: `https://docs.example.com/manual/`
   - Make sure to include the protocol (`http://` or `https://`)
   - Include paths to specific sections if you only want a subset of the documentation

2. **Select Output Format**: Choose your preferred output format:
   - **Single Markdown File**: Combines all pages into one large Markdown document with a table of contents
   - **ZIP Archive**: Creates individual Markdown files for each page, preserving the original structure

3. **Start Conversion**: Click the "Convert Site" button to begin the process.

4. **Monitor Progress**: The conversion process has several stages:
   - Crawling: Discovering and mapping all pages
   - Processing: Converting HTML to Markdown
   - Packaging: Preparing the output file

5. **Download**: When conversion is complete, click the "Download" button to save your file.
   - **Important**: Files are automatically deleted after 1 hour, so download promptly

## Advanced Options

Click the "Advanced Options" toggle to access additional settings:

### Path Prefix

Limit crawling to a specific section of a documentation site by providing a path prefix.

- Example: For `https://docs.example.com/product/v2/guide/`, entering `/product/v2/` will only crawl pages within that path.

### Include/Exclude Patterns

Fine-tune which pages get processed using regular expressions:

- **Include Pattern**: Only pages matching this pattern will be processed
  - Example: `tutorials|guides` will only include pages with "tutorials" or "guides" in the URL

- **Exclude Pattern**: Pages matching this pattern will be skipped
  - Example: `api-reference|changelog` will skip API reference and changelog pages

### Markdown Options

- **Include Frontmatter**: When enabled, adds YAML frontmatter with metadata to each page (title, original URL, conversion date)
- **Preserve Images**: When enabled, downloads and embeds images (as base64) in the Markdown files

## Troubleshooting

### Common Issues

#### Conversion Takes Too Long

- **Issue**: The conversion process seems stuck or is taking a very long time.
- **Solution**: 
  - Large documentation sites may take several minutes to process
  - Try limiting the scope with path prefixes or include/exclude patterns
  - Check if the site has a very large number of pages (>500)

#### Some Content Missing or Incorrectly Formatted

- **Issue**: Certain elements like tables, code blocks, or math equations don't convert correctly.
- **Solution**:
  - OmniDocs works best with standard HTML elements
  - Some dynamic content (JavaScript-generated) may not be captured
  - Report specific issues through our feedback form for improvement

#### Error: "Couldn't Access URL"

- **Issue**: OmniDocs reports it cannot access the provided URL.
- **Solution**:
  - Verify the URL is correct and publicly accessible
  - Some sites block automated crawlers - try a different documentation site
  - Check if the site requires authentication

## FAQ

### How does OmniDocs work?

OmniDocs uses a specialized crawler that identifies documentation page structures and navigation. It follows links within the same domain, extracts the content, and converts it to Markdown using advanced HTML-to-Markdown conversion techniques.

### Is there a limit to how many pages OmniDocs can process?

While there's no hard limit, performance may decrease with very large sites (1000+ pages). For optimal performance, we recommend using path prefixes to target specific sections of documentation when possible.

### Can OmniDocs handle password-protected documentation?

Currently, OmniDocs does not support sites that require authentication. It can only process publicly accessible documentation.

### Why are my files deleted after 1 hour?

To manage server resources and storage costs, we automatically delete all converted files after 1 hour. This ensures the service remains available and free for everyone. Make sure to download your files promptly after conversion.

### Can I use OmniDocs for commercial purposes?

Yes! OmniDocs is available for both personal and commercial use. Please refer to our license for details.

### How do I report issues or suggest improvements?

We welcome your feedback! Please submit issues or suggestions through our GitHub repository or contact form on the website. 