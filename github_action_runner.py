import argparse
import asyncio
import os
import hashlib
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app import ALLOWED_EXTENSIONS, OLLAMA_URL, process_file


def run(cmd, cwd=None):
    print(f"$ {' '.join(cmd)}")
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    subprocess.run(cmd, cwd=cwd, check=True, env=env)


def normalize_repo_url(url: str, token: str | None = None, username: str = "x-access-token") -> str:
    if not url.startswith("https://"):
        raise ValueError("Repository URLs must use HTTPS.")
    if token and "@" not in url:
        return url.replace("https://", f"https://{username}:{token}@", 1)
    return url


def clone_repo(url: str, target: Path, branch: str | None = None, token: str | None = None, username: str = "x-access-token", label: str = "repository"):
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([normalize_repo_url(url, token, username), str(target)])
    try:
        run(cmd)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Failed to clone {label}. If this repo is private or in another org, set an appropriate token secret "
            f"for that repo and pass it to the action runner. Git exited with code {exc.returncode}."
        ) from exc




def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

async def convert_repo(source_dir: Path, instructions: str):
    files_to_process: list[Path] = []
    original_hashes: dict[Path, str] = {}
    for root, _, files in os.walk(source_dir):
        for filename in files:
            fp = Path(root) / filename
            if fp.suffix.lower() in ALLOWED_EXTENSIONS:
                files_to_process.append(fp)
                original_hashes[fp] = file_sha256(fp)

    async with httpx.AsyncClient(headers={"User-Agent": "repo-converter-action/1.0"}) as client:
        results = []
        for fp in files_to_process:
            results.append(await process_file(str(fp), instructions, None, client, str(source_dir)))

    if not all(results):
        raise RuntimeError("One or more files failed during AI conversion. Aborting before commit/push.")

    # Remove untouched source/context files so only converted output is published.
    for fp in files_to_process:
        if fp.exists() and file_sha256(fp) == original_hashes[fp]:
            fp.unlink()


def reset_destination_repo(dest: Path):
    for item in dest.iterdir():
        if item.name in {".git", "README.md"}:
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()


def copy_tree_contents(src: Path, dest: Path):
    for item in src.iterdir():
        if item.name in {".git", "README.md"}:
            continue
        target = dest / item.name
        if item.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)




def validate_runtime_configuration():
    if not (OLLAMA_URL.startswith("http://") or OLLAMA_URL.startswith("https://")):
        raise ValueError(
            f"Invalid OLLAMA_URL: '{OLLAMA_URL}'. Set OLLAMA_URL to a full http(s) endpoint, "
            "for example http://ollama:11434/api/generate."
        )



def validate_ollama_reachability():
    parsed = urlparse(OLLAMA_URL)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        raise ValueError(f"Invalid OLLAMA_URL host in '{OLLAMA_URL}'.")

    try:
        socket.getaddrinfo(host, port)
    except socket.gaierror as exc:
        raise RuntimeError(
            "OLLAMA_URL is not reachable from this runner (DNS resolution failed). "
            "If you are using GitHub-hosted runners, 'http://ollama:11434' will not resolve. "
            "Set OLLAMA_URL to a publicly reachable or self-hosted reachable endpoint."
        ) from exc

def main():
    parser = argparse.ArgumentParser(description="Convert a source repository and publish to a destination repository.")
    parser.add_argument("--context-repo", required=True, help="HTTPS URL of source repository")
    parser.add_argument("--destination-repo", required=True, help="HTTPS URL of destination repository")
    parser.add_argument("--destination-stack", required=True, help="Target technology stack")
    parser.add_argument("--github-username", default=os.getenv("GITHUB_USERNAME", "x-access-token"), help="GitHub username for HTTPS auth")
    parser.add_argument("--branch", default="main", help="Destination branch")
    parser.add_argument("--context-token", default=os.getenv("CONTEXT_REPO_TOKEN") or os.getenv("GITHUB_TOKEN"), help="Token for cloning context repo (optional for public repos)")
    parser.add_argument("--destination-token", default=os.getenv("DESTINATION_REPO_TOKEN") or os.getenv("GITHUB_TOKEN"), help="Token for destination clone/push")
    args = parser.parse_args()

    validate_runtime_configuration()
    validate_ollama_reachability()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source_dir = tmp_path / "context_repo"
        dest_dir = tmp_path / "destination_repo"

        clone_repo(args.context_repo, source_dir, token=args.context_token, username=args.github_username, label="context repository")
        clone_repo(args.destination_repo, dest_dir, branch=args.branch, token=args.destination_token, username=args.github_username, label="destination repository")

        instructions = (
            f"Convert this repository to {args.destination_stack}. "
            "Return complete production-ready files with configs, tests, and docs as needed."
        )
        asyncio.run(convert_repo(source_dir, instructions))

        reset_destination_repo(dest_dir)
        copy_tree_contents(source_dir, dest_dir)

        run(["git", "config", "user.name", "github-actions[bot]"], cwd=str(dest_dir))
        run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=str(dest_dir))
        run(["git", "add", "."], cwd=str(dest_dir))

        status = subprocess.run(["git", "status", "--porcelain"], cwd=str(dest_dir), check=True, capture_output=True, text=True)
        if not status.stdout.strip():
            print("No changes detected after conversion; nothing to commit.")
            return

        run(["git", "commit", "-m", f"AI conversion to {args.destination_stack}"], cwd=str(dest_dir))
        run(["git", "push", "origin", args.branch], cwd=str(dest_dir))


if __name__ == "__main__":
    main()
