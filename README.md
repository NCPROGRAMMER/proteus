# Local AI Code Refactor Bot

A local, containerized web application that uses a Large Language Model (LLM) to refactor, document, or transform code projects. It runs entirely offline using Ollama and a Python FastAPI backend.

## Features

- **Local by Default:** Refactoring runs against your local Ollama model.
- **Optional Web Research:** If enabled, the app fetches public web snippets to enrich context.
- **Stack-Aware Prompting:** Loads framework/language context from JSON files in `context/` using extension and filename mapping rules.
- **Zip or Single File Input:** Upload either a source code zip or one supported code/config file, and get a refactored zip back.
- **Multi-File Aware:** Can generate entirely new project structures (e.g., Python to Java ports).
- **Hardware Agnostic:** Runs on standard Linux servers (CPU) or NVIDIA-equipped workstations (GPU).

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) or Docker Engine
- **RAM:** 16GB Minimum (for CPU mode)
- **GPU (Optional):** NVIDIA GPU with 12GB+ VRAM for accelerated processing

## Quick Start

### 1. Choose your Mode

**Option A: Standard / CPU Mode (Linux Servers)**
Use the standard compose file. This is best for servers without dedicated GPUs.
```bash
docker compose up --build -d
```

**Option B: NVIDIA GPU Mode (Local Workstation)**
Use the GPU-optimized compose file.
```bash
docker compose -f docker-compose.gpu.yml up --build -d
```

### 2. Download the Model
You must download the model once after starting the containers. We use `qwen2.5-coder:14b` for the best balance of logic and speed.

**Linux / Mac:**
```bash
docker exec -it ollama_backend ollama pull qwen2.5-coder:14b
```

**Windows (Git Bash):**
```bash
winpty docker exec -it ollama_backend ollama pull qwen2.5-coder:14b
```

### 3. Usage

1.  Open your browser to `http://localhost:8000`.
2.  Upload either a `.zip` project archive or one supported source/config file.
3.  Enter instructions (e.g., *"Add Typescript interfaces"* or *"Convert to Java Spring Boot"*).
4.  (Optional) Enable **Add web research to prompt context**.
5.  (Optional) Provide a custom query and web snippet count (1-10).
6.  Click **Process**.
7.  Wait for processing to finish (logs are available via `docker compose logs -f app`) and download the result.

## Project Structure

```text
.
├── app.py                 # FastAPI backend & LLM logic
├── Dockerfile             # Python environment build
├── docker-compose.yml     # Standard CPU/Universal config
├── docker-compose.gpu.yml # NVIDIA GPU config
├── requirements.txt       # Python dependencies
├── context/              # Stack context JSON files + mapper
│   ├── mapper.json       # Extension/filename to context-file mapping
│   └── *.json            # Detailed stack guidance and docs links
└── templates/
    └── index.html         # Frontend UI
```

## Troubleshooting

-   **Logs:** Run `docker compose logs -f app` to see what the AI is writing in real-time.
-   **Timeout:** The application is configured to wait indefinitely for the LLM. If you experience network timeouts (e.g. Nginx 504 Gateway Time-out), check your reverse proxy settings.
-   **Memory:** If the container crashes on large files, try a smaller model like `qwen2.5-coder:7b`.
