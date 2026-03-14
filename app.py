import os
import shutil
import zipfile
import tempfile
import asyncio
import httpx
import re
import json
from html import unescape
from typing import List, Dict, Any, Optional
from urllib.parse import quote_plus

from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Configuration
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "ollama")
OLLAMA_PORT = os.getenv("OLLAMA_PORT", "11434")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", f"http://{OLLAMA_HOST}:{OLLAMA_PORT}")
OLLAMA_GENERATE_PATH = os.getenv("OLLAMA_GENERATE_PATH", "/api/generate")
OLLAMA_URL = f"{OLLAMA_BASE_URL.rstrip('/')}/{OLLAMA_GENERATE_PATH.lstrip('/')}"
OLLAMA_TAGS_URL = f"{OLLAMA_BASE_URL.rstrip('/')}/api/tags"

# Default quality model for larger, multi-file conversions.
MODEL_NAME = os.getenv("MODEL_NAME", "qwen2.5-coder:14b")
# Faster model profile for local workstation throughput.
FAST_MODEL_NAME = os.getenv("FAST_MODEL_NAME", "qwen2.5-coder:7b")
MAX_WEB_SNIPPET_CHARS = 320
PROFILE_NAME = os.getenv("PROTEUS_PROFILE", "balanced").strip().lower()
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
MAX_FILE_CHARS = int(os.getenv("MAX_FILE_CHARS", "20000"))

ALLOWED_EXTENSIONS = {
    '.py', '.pyi', '.ipynb',
    '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.vue', '.svelte',
    '.html', '.htm', '.css', '.scss', '.sass', '.less',
    '.java', '.kt', '.kts', '.groovy',
    '.cpp', '.cc', '.cxx', '.c', '.h', '.hpp',
    '.cs', '.fs', '.rs', '.go', '.php', '.rb', '.swift', '.scala',
    '.json', '.jsonc', '.yaml', '.yml', '.toml', '.ini', '.env',
    '.xml', '.xsd', '.xsl', '.sql', '.graphql', '.gql',
    '.sh', '.bash', '.zsh', '.ps1', '.bat',
    '.dockerfile', '.tf', '.tfvars', '.md', '.txt',
}

CONTEXT_DIR = os.path.join(os.path.dirname(__file__), "context")
MAPPER_PATH = os.path.join(CONTEXT_DIR, "mapper.json")


def _load_context_mapper() -> Dict[str, Any]:
    try:
        with open(MAPPER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Unable to load context mapper: {type(e).__name__} - {e}")
        return {}


def _load_context_payload(file_name: str) -> Dict[str, Any]:
    path = os.path.join(CONTEXT_DIR, file_name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Unable to load context file {file_name}: {type(e).__name__} - {e}")
        return {}


def _pick_context_files(filename: str, ext: str, instructions: str, mapper: Dict[str, Any]) -> List[str]:
    extension_map = mapper.get("extension_map", {})
    filename_map = mapper.get("filename_map", {})
    keyword_priority = mapper.get("instruction_keyword_priority", {})
    max_files = int(mapper.get("max_context_files_per_prompt", 2) or 2)

    candidates: List[str] = []
    for file_name in filename_map.get(filename.lower(), []):
        if file_name not in candidates:
            candidates.append(file_name)
    for file_name in extension_map.get(ext, []):
        if file_name not in candidates:
            candidates.append(file_name)

    if not candidates:
        return []

    lowered_instructions = (instructions or "").lower()
    ranked: List[str] = []
    for context_file in candidates:
        keywords = keyword_priority.get(context_file, [])
        if any(keyword in lowered_instructions for keyword in keywords):
            ranked.append(context_file)

    ordered = ranked + [c for c in candidates if c not in ranked]
    return ordered[:max_files]


def build_stack_context(user_instructions: str, filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    mapper = _load_context_mapper()
    context_files = _pick_context_files(filename, ext, user_instructions, mapper)
    if not context_files:
        return ""

    lines = ["TECH STACK CONVERSION GUIDANCE (extension mapped):"]
    for context_file in context_files:
        payload = _load_context_payload(context_file)
        if not payload:
            continue

        root_key = next(iter(payload.keys()))
        details = payload.get(root_key, {})
        stack = details.get("stack", context_file)
        lines.append(f"- Context file: context/{context_file} (root extension key: {root_key}, stack: {stack})")

        description = details.get("description", "").strip()
        if description:
            lines.append(f"  Description: {description}")

        for item in details.get("conversion_guidance", [])[:6]:
            lines.append(f"  Guidance: {item}")

        docs = details.get("documentation_links", [])[:4]
        if docs:
            lines.append("  Docs:")
            for doc in docs:
                title = doc.get("title", "Reference")
                url = doc.get("url", "")
                lines.append(f"    - {title}: {url}")

    return "\n".join(lines)


def _flatten_related_topics(items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """DuckDuckGo can nest topics under a "Topics" key; flatten for easier parsing."""
    flat: List[Dict[str, str]] = []
    for item in items:
        if "Topics" in item:
            flat.extend(_flatten_related_topics(item.get("Topics", [])))
            continue

        text = (item.get("Text") or "").strip()
        url = (item.get("FirstURL") or "").strip()
        if text and url:
            flat.append({"text": text, "url": url})
    return flat


def _trim_snippet(text: str, max_chars: int = MAX_WEB_SNIPPET_CHARS) -> str:
    compact = " ".join(unescape(text).split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"


async def _search_duckduckgo_instant(
    client: httpx.AsyncClient,
    query: str,
    max_results: int,
) -> List[Dict[str, str]]:
    """Lightweight metadata search from Instant Answer API."""
    response = await client.get(
        "https://api.duckduckgo.com/",
        params={
            "q": query,
            "format": "json",
            "no_redirect": 1,
            "no_html": 1,
            "skip_disambig": 1,
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()

    snippets: List[Dict[str, str]] = []

    abstract = _trim_snippet((data.get("AbstractText") or "").strip())
    abstract_url = (data.get("AbstractURL") or "").strip()
    if abstract:
        snippets.append({
            "title": data.get("Heading") or "DuckDuckGo Abstract",
            "url": abstract_url,
            "snippet": abstract,
        })

    related_topics = _flatten_related_topics(data.get("RelatedTopics", []))
    for topic in related_topics:
        if len(snippets) >= max_results:
            break
        snippets.append({
            "title": "DuckDuckGo Related Topic",
            "url": topic["url"],
            "snippet": _trim_snippet(topic["text"]),
        })

    return snippets[:max_results]


async def _search_duckduckgo_html(
    client: httpx.AsyncClient,
    query: str,
    max_results: int,
) -> List[Dict[str, str]]:
    """Fallback to DDG lightweight HTML endpoint for broader result coverage."""
    encoded = quote_plus(query)
    url = f"https://duckduckgo.com/html/?q={encoded}"
    response = await client.get(url, timeout=20)
    response.raise_for_status()
    html = response.text

    pattern = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
        r'<a[^>]*class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
        re.DOTALL,
    )

    parsed: List[Dict[str, str]] = []
    for match in pattern.finditer(html):
        if len(parsed) >= max_results:
            break
        title = _trim_snippet(re.sub(r"<[^>]+>", "", match.group("title")))
        snippet = _trim_snippet(re.sub(r"<[^>]+>", "", match.group("snippet")))
        href = unescape(match.group("href"))
        parsed.append({"title": title, "url": href, "snippet": snippet})

    return parsed


async def fetch_web_context(
    client: httpx.AsyncClient,
    query: str,
    max_results: int = 5,
) -> str:
    """
    Fetch concise web context and return a compact text block safe to include in model context.
    Attempts Instant Answer first, then HTML search fallback.
    """
    query = (query or "").strip()
    if not query:
        return ""

    snippets: List[Dict[str, str]] = []

    try:
        snippets = await _search_duckduckgo_instant(client, query, max_results)
    except Exception as e:
        print(f"⚠️ DDG instant search failed ({type(e).__name__}): {e}")

    if len(snippets) < max_results:
        try:
            html_results = await _search_duckduckgo_html(client, query, max_results)
            seen_urls = {s.get("url", "") for s in snippets}
            for result in html_results:
                if result.get("url", "") in seen_urls:
                    continue
                snippets.append(result)
                if len(snippets) >= max_results:
                    break
        except Exception as e:
            print(f"⚠️ DDG HTML search failed ({type(e).__name__}): {e}")

    if not snippets:
        return ""

    lines = [
        "WEB RESEARCH (use only if relevant and technically sound; cite sources in comments/docs if used):"
    ]
    for idx, item in enumerate(snippets, start=1):
        source = item.get("url") or "(no-url)"
        title = item.get("title") or "Untitled"
        snippet = item.get("snippet") or ""
        lines.append(f"{idx}. [{title}] {snippet} (source: {source})")

    return "\n".join(lines)


def _is_safe_output_filename(fname: str) -> bool:
    candidate = (fname or "").strip()
    if not candidate:
        return False
    if "\x00" in candidate or ".." in candidate:
        return False
    if candidate.startswith(("/", "\\")):
        return False
    if "\n" in candidate or "\r" in candidate or len(candidate) > 240:
        return False
    parts = [p for p in candidate.replace("\\", "/").split("/") if p]
    if not parts:
        return False
    leaf = parts[-1]
    if leaf.lower() in {"file", "path", "filename", "name"}:
        return False
    if " " in leaf and "." not in leaf:
        return False
    return True


def parse_and_save_files(raw_text: str, base_dir: str):
    """
    Scans for '### FILE: <name>' markers.
    If found, saves multiple files. Returns list of created files.
    """
    segments = re.split(r'### FILE:\s*([^\n]+)\n', raw_text)

    if len(segments) < 3:
        return None

    created_files = []

    for i in range(1, len(segments), 2):
        fname = segments[i].strip()
        content = segments[i + 1]

        if content.strip().startswith("```"):
            lines = content.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            content = "\n".join(lines)
        if not _is_safe_output_filename(fname):
            print(f"⚠️ Skipping unsafe filename: {fname}")
            continue

        full_path = os.path.join(base_dir, fname)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

        created_files.append(fname)

    return created_files


def _resolve_profile(requested_mode: Optional[str], source_char_count: int) -> str:
    mode = (requested_mode or PROFILE_NAME or "balanced").strip().lower()

    if mode not in {"speed", "balanced", "quality", "auto"}:
        mode = "balanced"

    if mode == "auto":
        return "speed" if source_char_count <= 6000 else "balanced"

    return mode


def _build_generation_config(mode: str) -> Dict[str, Any]:
    profiles = {
        "speed": {
            "model": FAST_MODEL_NAME,
            "options": {
                "temperature": 0.1,
                "num_ctx": 4096,
                "num_predict": 2048,
            },
        },
        "balanced": {
            "model": MODEL_NAME,
            "options": {
                "temperature": 0.2,
                "num_ctx": 8192,
                "num_predict": 3072,
            },
        },
        "quality": {
            "model": MODEL_NAME,
            "options": {
                "temperature": 0.2,
                "num_ctx": 16384,
                "num_predict": 4096,
            },
        },
    }
    return profiles.get(mode, profiles["balanced"])


def _is_conversion_request(instructions: str) -> bool:
    lowered = (instructions or "").lower()
    keywords = ["convert", "port", "rewrite", "migrate", "translate", "to java", "to spring"]
    return any(k in lowered for k in keywords)


def _looks_like_non_code_text(text: str) -> bool:
    lowered = (text or "").lower()
    phrases = [
        "potential improvements",
        "example usage",
        "this script can be extended",
        "to run this application",
    ]
    return any(p in lowered for p in phrases)


def _safe_relpath(path: str, base_dir: str) -> str:
    return os.path.relpath(path, base_dir).replace(os.sep, "/")


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


async def _validate_ollama_setup(client: httpx.AsyncClient) -> Dict[str, Any]:
    """Return {'error': str|None, 'models': set[str]} for Ollama availability/model checks."""
    try:
        tags_resp = await client.get(OLLAMA_TAGS_URL, timeout=20)
    except Exception as e:
        return {
            "error": (
                "Unable to reach Ollama at "
                f"{OLLAMA_BASE_URL}. Check docker networking and that the ollama container is running. "
                f"({type(e).__name__}: {e})"
            ),
            "models": set(),
        }

    if tags_resp.status_code >= 400:
        return {
            "error": (
                f"Ollama is reachable but returned HTTP {tags_resp.status_code} from /api/tags. "
                "Check your OLLAMA_BASE_URL/OLLAMA_HOST configuration."
            ),
            "models": set(),
        }

    try:
        payload = tags_resp.json()
    except Exception:
        payload = {}

    models = payload.get("models", []) if isinstance(payload, dict) else []
    model_names = {m.get("name", "") for m in models if isinstance(m, dict)}

    if MODEL_NAME not in model_names and FAST_MODEL_NAME not in model_names:
        return {
            "error": (
                "No configured model is available in Ollama. "
                f"Expected at least one of: {MODEL_NAME}, {FAST_MODEL_NAME}. "
                "Pull a model first, for example: "
                f"docker exec -it ollama_backend ollama pull {MODEL_NAME}"
            ),
            "models": model_names,
        }

    return {"error": None, "models": model_names}


def _resolve_mode_for_available_models(mode: str, available_models: set) -> Dict[str, Optional[str]]:
    desired = _build_generation_config(mode).get("model")
    if desired in available_models:
        return {"mode": mode, "warning": None, "error": None}

    if mode == "auto":
        if FAST_MODEL_NAME in available_models:
            return {
                "mode": "speed",
                "warning": (
                    f"Auto mode selected unavailable model '{desired}'. "
                    f"Using speed mode with '{FAST_MODEL_NAME}'."
                ),
                "error": None,
            }
        if MODEL_NAME in available_models:
            return {
                "mode": "balanced",
                "warning": (
                    f"Auto mode selected unavailable model '{desired}'. "
                    f"Using balanced mode with '{MODEL_NAME}'."
                ),
                "error": None,
            }

    return {
        "mode": mode,
        "warning": None,
        "error": (
            f"Selected mode '{mode}' requires model '{desired}', which is not available locally. "
            f"Pull it with: docker exec -it ollama_backend ollama pull {desired}"
        ),
    }


async def process_file(
    file_path: str,
    user_instructions: str,
    web_context: Optional[str],
    client: httpx.AsyncClient,
    extract_root: str,
    mode: str,
) -> List[str]:
    filename = os.path.basename(file_path)
    if os.path.splitext(filename)[1].lower() not in ALLOWED_EXTENSIONS:
        return []

    try:
        content = _read_text_file(file_path)
    except Exception:
        return []

    if len(content) > MAX_FILE_CHARS:
        print(
            f"⚠️ Input {filename} is {len(content)} chars. Truncating to first {MAX_FILE_CHARS} chars for faster processing."
        )
        content = content[:MAX_FILE_CHARS]

    generation = _build_generation_config(mode)
    conversion_request = _is_conversion_request(user_instructions)

    system_prompt = (
        "You are an expert software architect. "
        "If the user asks to PORT or REWRITE code into a new language/framework that requires multiple files (like Spring Boot), "
        "you MUST output every single file needed for the new project.\n\n"
        "STRICT OUTPUT FORMAT:\n"
        "To create a file, start with: ### FILE: <path/to/filename>\n"
        "Followed immediately by the code for that file.\n"
        "Example:\n"
        "### FILE: pom.xml\n"
        "<project>...</project>\n"
        "### FILE: src/main/java/com/example/App.java\n"
        "package com.example;\n"
        "...\n\n"
        "Do not output conversational text. Do not output summaries. Just file markers and code."
    )

    user_prompt = (
        f"CURRENT FILE: {filename}\n"
        f"INSTRUCTION: {user_instructions}\n"
        f"{build_stack_context(user_instructions, filename)}\n"
        f"{(web_context or '').strip()}\n\n"
        f"CONTENT:\n{content}"
    )

    async def _generate(prompt_text: str) -> str:
        response = await client.post(
            OLLAMA_URL,
            json={
                "model": generation["model"],
                "prompt": prompt_text,
                "system": system_prompt,
                "stream": False,
                "keep_alive": OLLAMA_KEEP_ALIVE,
                "options": generation["options"],
            },
            timeout=None,
        )
        response.raise_for_status()
        return response.json().get("response", "")

    try:
        raw_output = await _generate(user_prompt)
        new_files = None
        if raw_output.lstrip().startswith("### FILE:"):
            new_files = parse_and_save_files(raw_output, extract_root)

        if conversion_request and not new_files:
            retry_prompt = user_prompt + (
                "\n\nIMPORTANT: You must return ONLY code files using '### FILE:' markers. "
                "No explanations, no markdown prose."
            )
            raw_output = await _generate(retry_prompt)
            new_files = parse_and_save_files(raw_output, extract_root)

        if new_files:
            print(f"✅ Converted {filename} into {len(new_files)} new files:")
            for nf in new_files:
                print(f"   -> {nf}")
            return new_files

        cleaned_code = raw_output.strip()
        if cleaned_code.startswith("```"):
            cleaned_code = cleaned_code.split("\n", 1)[1].rsplit("\n", 1)[0]

        if conversion_request and _looks_like_non_code_text(cleaned_code):
            print(f"⚠️ Skipping non-code model output for {filename}")
            return []

        if len(cleaned_code) > 0 and "I cannot assist" not in cleaned_code:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(cleaned_code)
            rel = _safe_relpath(file_path, extract_root)
            print(f"✅ Refactored {filename} (Single file update)")
            return [rel]

    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        body = ""
        try:
            body = e.response.text if e.response is not None else ""
        except Exception:
            body = ""

        if status == 404 and "not found" in body.lower() and "model" in body.lower():
            print(
                "❌ Ollama model not found. "
                f"Tried model='{generation['model']}'. "
                f"Run: docker exec -it ollama_backend ollama pull {generation['model']}"
            )
        else:
            print(f"❌ Error processing {filename}: HTTP {status} from Ollama - {body[:300]}")

    except Exception as e:
        import traceback

        print(f"❌ Error processing {filename}: {type(e).__name__} - {e}")
        traceback.print_exc()

    return []


def _collect_uploads(file: Optional[UploadFile], files: Optional[List[UploadFile]]) -> List[UploadFile]:
    uploads: List[UploadFile] = []
    if file and (file.filename or "").strip():
        uploads.append(file)
    if files:
        for item in files:
            if item and (item.filename or "").strip():
                uploads.append(item)
    return uploads


def _prepare_upload_inputs(uploads: List[UploadFile], work_dir: str, extract_dir: str) -> Optional[Dict[str, str]]:
    if not uploads:
        return {"error": "No files uploaded. Add one or more files, or a zip."}

    for idx, upload in enumerate(uploads):
        upload_name = upload.filename or f"uploaded_input_{idx}"
        staged_path = os.path.join(work_dir, f"upload_{idx}_{os.path.basename(upload_name)}")

        with open(staged_path, "wb") as f:
            shutil.copyfileobj(upload.file, f)

        uploaded_ext = os.path.splitext(upload_name)[1].lower()
        if uploaded_ext == ".zip":
            with zipfile.ZipFile(staged_path, 'r') as z:
                z.extractall(extract_dir)
            continue

        if uploaded_ext not in ALLOWED_EXTENSIONS:
            return {
                "error": (
                    f"Unsupported file type: {uploaded_ext or 'unknown'}. "
                    "Upload a .zip or supported source/config file(s)."
                )
            }

        safe_name = os.path.basename(upload_name or f"uploaded{uploaded_ext}")
        shutil.copy2(staged_path, os.path.join(extract_dir, safe_name))

    return None


def _discover_files(extract_dir: str) -> List[str]:
    files_to_process: List[str] = []
    for root, _, files in os.walk(extract_dir):
        for filename in files:
            files_to_process.append(os.path.join(root, filename))
    return files_to_process


def _estimate_total_chars(files_to_process: List[str]) -> int:
    total_chars = 0
    for fp in files_to_process:
        try:
            total_chars += len(_read_text_file(fp))
        except Exception:
            continue
    return total_chars


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/refactor")
async def refactor_endpoint(
    background_tasks: BackgroundTasks,
    file: Optional[UploadFile] = File(None),
    files: Optional[List[UploadFile]] = File(None),
    instructions: str = Form(...),
    web_search_enabled: bool = Form(False),
    web_search_query: str = Form(""),
    web_search_results: int = Form(5),
    performance_mode: str = Form("auto"),
):
    work_dir = tempfile.mkdtemp()
    extract_dir = os.path.join(work_dir, "source")
    output_zip = os.path.join(work_dir, "refactored.zip")

    os.makedirs(extract_dir, exist_ok=True)

    try:
        uploads = _collect_uploads(file, files)
        input_error = _prepare_upload_inputs(uploads, work_dir, extract_dir)
        if input_error:
            return input_error

        files_to_process = _discover_files(extract_dir)
        if not files_to_process:
            return {"error": "No processable files were found in the upload."}
        total_chars = _estimate_total_chars(files_to_process)
        mode = _resolve_profile(performance_mode, total_chars)
        print(f"⚙️ Processing mode: {mode} (total source chars: {total_chars})")

        sem = asyncio.Semaphore(max(1, int(os.getenv("OLLAMA_CONCURRENCY", "1"))))

        async with httpx.AsyncClient(
            headers={
                "User-Agent": "local-chat/1.0 (+https://localhost)",
            }
        ) as client:
            setup = await _validate_ollama_setup(client)
            if setup.get("error"):
                return {"error": setup["error"]}

            resolved = _resolve_mode_for_available_models(mode, setup.get("models", set()))
            if resolved.get("error"):
                return {"error": resolved["error"]}
            mode = resolved.get("mode") or mode
            if resolved.get("warning"):
                print(f"⚠️ {resolved['warning']}")

            search_query = (web_search_query or "").strip() or instructions
            web_context = ""
            if web_search_enabled:
                bounded_results = max(1, min(web_search_results, 10))
                web_context = await fetch_web_context(
                    client,
                    search_query,
                    max_results=bounded_results,
                )
                if web_context:
                    print(f"✅ Web context added to prompt ({bounded_results} results)")
                else:
                    print("⚠️ Web search enabled, but no context was found")

            async def worker(fp, instr, web_ctx, async_client):
                async with sem:
                    return await process_file(fp, instr, web_ctx, async_client, extract_dir, mode)

            tasks = [worker(fp, instructions, web_context, client) for fp in files_to_process]
            results = await asyncio.gather(*tasks)
            total_changed = sum(len(item or []) for item in results)
            if total_changed == 0:
                return {
                    "error": (
                        "No converted files were produced. The model may have returned non-code guidance text. "
                        "Try Speed mode or refine instructions (e.g., 'Output only code files with ### FILE markers')."
                    )
                }

        with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(extract_dir):
                for filename in files:
                    file_path = os.path.join(root, filename)
                    arcname = os.path.relpath(file_path, extract_dir)
                    z.write(file_path, arcname)

        return FileResponse(output_zip, filename="converted_project.zip", media_type="application/zip")

    except Exception as e:
        return {"error": str(e)}

    finally:
        background_tasks.add_task(shutil.rmtree, work_dir)


@app.post("/refactor-stream")
async def refactor_stream_endpoint(
    file: Optional[UploadFile] = File(None),
    files: Optional[List[UploadFile]] = File(None),
    instructions: str = Form(...),
    web_search_enabled: bool = Form(False),
    web_search_query: str = Form(""),
    web_search_results: int = Form(5),
    performance_mode: str = Form("auto"),
):
    work_dir = tempfile.mkdtemp()
    extract_dir = os.path.join(work_dir, "source")
    os.makedirs(extract_dir, exist_ok=True)

    uploads = _collect_uploads(file, files)
    input_error = _prepare_upload_inputs(uploads, work_dir, extract_dir)

    async def stream_results():
        try:
            if input_error:
                yield json.dumps({"type": "error", "message": input_error.get("error", "Invalid input")}) + "\n"
                return

            files_to_process = _discover_files(extract_dir)
            if not files_to_process:
                yield json.dumps({"type": "error", "message": "No processable files were found in the upload."}) + "\n"
                return
            total_chars = _estimate_total_chars(files_to_process)
            mode = _resolve_profile(performance_mode, total_chars)

            async with httpx.AsyncClient(
                headers={
                    "User-Agent": "local-chat/1.0 (+https://localhost)",
                }
            ) as client:
                setup = await _validate_ollama_setup(client)
                if setup.get("error"):
                    yield json.dumps({"type": "error", "message": setup["error"]}) + "\n"
                    return

                resolved = _resolve_mode_for_available_models(mode, setup.get("models", set()))
                mode = resolved.get("mode") or mode
                if resolved.get("warning"):
                    yield json.dumps({"type": "warning", "message": resolved["warning"]}) + "\n"

                yield json.dumps({"type": "start", "total_files": len(files_to_process), "mode": mode}) + "\n"

                search_query = (web_search_query or "").strip() or instructions
                web_context = ""
                if web_search_enabled:
                    bounded_results = max(1, min(web_search_results, 10))
                    web_context = await fetch_web_context(
                        client,
                        search_query,
                        max_results=bounded_results,
                    )

                total_saved = 0
                for idx, fp in enumerate(files_to_process, start=1):
                    changed_files = await process_file(fp, instructions, web_context, client, extract_dir, mode)
                    for rel_path in changed_files:
                        abs_path = os.path.join(extract_dir, rel_path)
                        if not os.path.exists(abs_path):
                            continue
                        try:
                            content = _read_text_file(abs_path)
                        except Exception:
                            continue
                        total_saved += 1
                        yield json.dumps({
                            "type": "file",
                            "path": rel_path,
                            "content": content,
                            "processed": idx,
                            "total": len(files_to_process),
                        }) + "\n"

                    yield json.dumps({"type": "progress", "processed": idx, "total": len(files_to_process)}) + "\n"

            if total_saved == 0:
                yield json.dumps({
                    "type": "error",
                    "message": "No converted files were produced. The model likely returned non-code guidance text. Try Speed mode or stricter conversion instructions."
                }) + "\n"
                return

            yield json.dumps({"type": "done", "message": "Conversion complete"}) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    return StreamingResponse(stream_results(), media_type="application/x-ndjson")
