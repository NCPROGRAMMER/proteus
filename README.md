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

### 2. Automatic Model Prefetch on `docker compose up`
The compose stack now includes an `ollama_model_init` service that automatically pulls models for:
- **Fast mode** (`FAST_MODEL_NAME`, default `qwen2.5-coder:7b`)
- **Balanced mode** (`BALANCED_MODEL_NAME`, default `qwen2.5-coder:14b`)
- **Quality mode** (`QUALITY_MODEL_NAME`, default `qwen2.5-coder:14b`)

You can override them before startup:
```bash
export FAST_MODEL_NAME=qwen2.5-coder:7b
export BALANCED_MODEL_NAME=qwen2.5-coder:14b
export QUALITY_MODEL_NAME=qwen2.5-coder:14b
docker compose up --build -d
```

You can monitor pulls with:
```bash
docker compose logs -f ollama_model_init
```

### 3. Usage

1.  Open your browser to `http://localhost:8000`.
2.  Upload one or more inputs: a `.zip` project archive, individual supported source/config files, or a mix of both.
3.  Enter instructions (e.g., *"Add Typescript interfaces"* or *"Convert to Java Spring Boot"*).
4.  (Optional) Enable **Add web research to prompt context**.
5.  (Optional) Provide a custom query and web snippet count (1-10).
6.  Click **Process**.
7.  Wait for processing to finish (logs are available via `docker compose logs -f app`) and download the result.

Multi-file upload is supported. If you choose several files and/or zips in one request, Proteus stages and processes them together in a single run.


## Performance Tuning (for 16GB RAM + decent GPU)

If your conversions are taking 10-15 minutes, switch to a faster profile and model:

1. Use the GPU compose profile:
```bash
docker compose -f docker-compose.gpu.yml up --build -d
```

2. Pull a faster model for day-to-day conversions:
```bash
docker exec -it ollama_backend ollama pull qwen2.5-coder:7b
```

3. Set runtime env vars for speed-focused runs (example):
```bash
export PROTEUS_PROFILE=auto
export FAST_MODEL_NAME=qwen2.5-coder:7b
export BALANCED_MODEL_NAME=qwen2.5-coder:14b
export QUALITY_MODEL_NAME=qwen2.5-coder:14b
export OLLAMA_CONCURRENCY=1
export OLLAMA_KEEP_ALIVE=30m
export MAX_FILE_CHARS=20000
export AUTO_PULL_MODELS=true
```

4. In the UI, choose **Performance mode = Speed** (or Auto).

### What these settings do
- `AUTO_PULL_MODELS=true` makes Proteus automatically pull missing configured models (`FAST_MODEL_NAME`, `BALANCED_MODEL_NAME`, `QUALITY_MODEL_NAME`) before conversion starts.
- `Speed` mode uses a smaller context window and lower output token cap to reduce generation latency.
- `Auto` mode selects `Speed` for smaller inputs and `Balanced` for larger ones.
- `MAX_FILE_CHARS` limits huge source files from overloading prompt size (improves local responsiveness).
- `Balanced` and `Quality` now require their configured model to be available locally. If the model is missing, Proteus returns a clear error with the exact `ollama pull` command. Auto mode may still fallback to an available model.
- If a model returns non-code guidance text instead of converted files, Proteus now stops with an explicit error instead of silently reporting success with zero saved files.

For Python-to-Java conversion of a simple script, Speed/Auto mode on GPU hardware should substantially reduce total runtime versus the default high-context profile.


## Direct-to-Folder Save (No Manual Download Click)

You can now skip the final download step in supported browsers:

1. Check **Save directly to local folder (Chrome/Edge)** in the UI.
2. Click **Choose Output Folder** and select a directory on your machine.
3. Start refactoring.
4. Proteus will stream generated files and write them into that folder as conversion progresses.

> Note: direct folder writes use the browser File System Access API (supported in Chromium browsers such as Chrome/Edge). Other browsers automatically fall back to ZIP download.

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
-   **404 from Ollama `/api/generate`:** This usually means the model is not pulled yet. Run `docker exec -it ollama_backend ollama pull qwen2.5-coder:14b` (or your configured `BALANCED_MODEL_NAME` / `QUALITY_MODEL_NAME`).
