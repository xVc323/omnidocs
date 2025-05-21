# OmniDocs

A powerful tool for automated documentation site crawling and Markdown conversion. **OmniDocs generates LLM-friendly Markdown files**—perfect for AI ingestion, semantic search, and knowledge base building. OmniDocs intelligently crawls documentation websites and exports them as well-formatted, structured Markdown files ready for use with large language models.

## 🌟 Features

- **Smart Crawling**: Automatically identifies and targets only documentation pages
- **Structured Conversion**: Preserves document hierarchy and navigation order
- **LLM-Optimized Output**: Produces clean, consistent Markdown ideal for AI/ML pipelines, RAG, and vector databases
- **Flexible Output**: Choose between single consolidated Markdown file or multi-file ZIP archive
- **High-Fidelity Markdown**: Accurately converts tables, code blocks, lists, and more
- **User-Friendly Interface**: Simple form with advanced options for customization
- **Responsive Design**: Works on desktop and mobile devices with dark mode support
- **Real-time Progress**: Live updates during conversion process
- **Temporary Storage**: Files automatically deleted after 1 hour (users notified)

## 🌐 Live Demo

Visit [omnidocs.pat.network](https://omnidocs.pat.network) to try OmniDocs now!

## 📋 User Guide

### Basic Usage

1. Enter the URL of the documentation site you want to convert
2. Click "Convert Site"
3. Wait for the conversion to complete (you'll see a progress indicator)
4. Download your converted documentation as either:
   - A single Markdown file (all_docs.md)
   - A ZIP archive containing individual Markdown files

### Advanced Options

- **Path Prefix**: Limit crawling to specific sections of a documentation site
- **Include/Exclude Patterns**: Fine-tune which pages get crawled using regex patterns
- **Output Format**: Choose between consolidated Markdown or multi-file ZIP

### Important Notes

- **Download Your Files Promptly**: All converted files are automatically deleted after 1 hour
- **Large Sites**: Complex documentation sites with many pages may take several minutes to process
- **Same-Domain Limitation**: OmniDocs only crawls pages within the same domain as the seed URL

## 🛠️ Installation

### Prerequisites

- Python 3.9 or higher
- Node.js 16 or higher
- Redis (for Celery task queue)
- Cloudflare R2 account or compatible S3 storage

### Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/xvc323/omnidocs.git
   cd omnidocs
   ```

2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Install frontend dependencies:
   ```bash
   cd frontend
   npm install
   cd ..
   ```

4. Set up environment variables (create a `.env` file based on `env.example`):
   ```
   R2_ACCOUNT_ID=your_account_id
   R2_ACCESS_KEY_ID=your_access_key
   R2_SECRET_ACCESS_KEY=your_secret_key
   R2_BUCKET_NAME=your_bucket_name
   ```

## 🚀 Running Locally

Start all services with the provided script:

```bash
./start-omnidocs.sh
```

Or start each component manually:

1. Start Redis (required for Celery):
   ```bash
   redis-server
   ```

2. Start Celery worker:
   ```bash
   celery -A celery_app worker --loglevel=info
   ```

3. Start Celery beat (for scheduled tasks):
   ```bash
   celery -A celery_app beat --loglevel=info
   ```

4. Start the API server:
   ```bash
   uvicorn api_main:app --reload
   ```

5. Start the frontend (in a separate terminal):
   ```bash
   cd frontend && npm run dev
   ```

6. Open your browser and navigate to `http://localhost:3000`

## 🐳 Docker Deployment

OmniDocs can be deployed using Docker:

```bash
docker-compose up -d
```

For Railway deployments, use the provided Railway configuration files in the `railway/` directory.

## 💻 API Endpoints

- `POST /convert` - Start a new conversion job
- `GET /download/{jobId}` - Download converted file
- `GET /api/jobs/{jobId}/events` - SSE endpoint for job progress updates

## 📄 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgements

- [FastAPI](https://fastapi.tiangolo.com/) - Web framework
- [Next.js](https://nextjs.org/) - Frontend framework
- [Pandoc](https://pandoc.org/) - Document conversion
- [Celery](https://docs.celeryq.dev/) - Task queue
