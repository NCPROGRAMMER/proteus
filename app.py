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
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Configuration
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434/api/generate")
# 14B is crucial here. 3B struggles to generate multi-file projects consistently.
MODEL_NAME = os.getenv("MODEL_NAME", "qwen2.5-coder:14b")
MAX_WEB_SNIPPET_CHARS = 320

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

        if ".." in fname or fname.startswith("/") or fname.startswith("\\"):
            print(f"⚠️ Skipping unsafe filename: {fname}")
            continue

        full_path = os.path.join(base_dir, fname)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

        created_files.append(fname)

    return created_files


async def process_file(
    file_path: str,
    user_instructions: str,
    web_context: Optional[str],
    client: httpx.AsyncClient,
    extract_root: str,
):
    filename = os.path.basename(file_path)
    if os.path.splitext(filename)[1].lower() not in ALLOWED_EXTENSIONS:
        return

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return

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
        "Do not output conversational text. Just the file markers and code."
    )

    user_prompt = (
        f"CURRENT FILE: {filename}\n"
        f"INSTRUCTION: {user_instructions}\n"
        f"{build_stack_context(user_instructions, filename)}\n"
        f"{(web_context or '').strip()}\n\n"
        f"CONTENT:\n{content}"
    )

    try:
        response = await client.post(
            OLLAMA_URL,
            json={
                "model": MODEL_NAME,
                "prompt": user_prompt,
                "system": system_prompt,
                "stream": False,
                "options": {
                    "temperature": 0.2,
                    "num_ctx": 16384,
                },
            },
            timeout=None,
        )
        response.raise_for_status()
        result = response.json()
        raw_output = result.get("response", "")

        new_files = parse_and_save_files(raw_output, extract_root)

        if new_files:
            print(f"✅ Converted {filename} into {len(new_files)} new files:")
            for nf in new_files:
                print(f"   -> {nf}")
        else:
            cleaned_code = raw_output.strip()
            if cleaned_code.startswith("```"):
                cleaned_code = cleaned_code.split("\n", 1)[1].rsplit("\n", 1)[0]

            if len(cleaned_code) > 0 and "I cannot assist" not in cleaned_code:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(cleaned_code)
                print(f"✅ Refactored {filename} (Single file update)")

    except Exception as e:
        import traceback

        print(f"❌ Error processing {filename}: {type(e).__name__} - {e}")
        traceback.print_exc()


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/refactor")
async def refactor_endpoint(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    instructions: str = Form(...),
    web_search_enabled: bool = Form(False),
    web_search_query: str = Form(""),
    web_search_results: int = Form(5),
):
    work_dir = tempfile.mkdtemp()
    uploaded_path = os.path.join(work_dir, file.filename or "uploaded_input")
    extract_dir = os.path.join(work_dir, "source")
    output_zip = os.path.join(work_dir, "refactored.zip")

    os.makedirs(extract_dir, exist_ok=True)

    try:
        with open(uploaded_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        uploaded_ext = os.path.splitext(file.filename or "")[1].lower()
        if uploaded_ext == ".zip":
            with zipfile.ZipFile(uploaded_path, 'r') as z:
                z.extractall(extract_dir)
        else:
            if uploaded_ext not in ALLOWED_EXTENSIONS:
                return {
                    "error": (
                        f"Unsupported file type: {uploaded_ext or 'unknown'}. "
                        "Upload a .zip or a supported source file extension."
                    )
                }
            safe_name = os.path.basename(file.filename or f"uploaded{uploaded_ext}")
            shutil.copy2(uploaded_path, os.path.join(extract_dir, safe_name))

        files_to_process = []
        for root, _, files in os.walk(extract_dir):
            for filename in files:
                files_to_process.append(os.path.join(root, filename))

        sem = asyncio.Semaphore(1)

        async with httpx.AsyncClient(
            headers={
                "User-Agent": "local-chat/1.0 (+https://localhost)",
            }
        ) as client:
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
                    await process_file(fp, instr, web_ctx, async_client, extract_dir)

            tasks = [worker(fp, instructions, web_context, client) for fp in files_to_process]
            await asyncio.gather(*tasks)

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
