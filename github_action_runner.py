import argparse
import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from app import ALLOWED_EXTENSIONS, process_file


def run(cmd, cwd=None):
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=cwd, check=True)


def normalize_repo_url(url: str, token: str | None = None) -> str:
    if not url.startswith("https://"):
        raise ValueError("Repository URLs must use HTTPS.")
    if token and "@" not in url:
        return url.replace("https://", f"https://x-access-token:{token}@", 1)
    return url


async def convert_repo(source_dir: Path, instructions: str):
    files_to_process = []
    for root, _, files in os.walk(source_dir):
        for filename in files:
            fp = Path(root) / filename
            if fp.suffix.lower() in ALLOWED_EXTENSIONS:
                files_to_process.append(str(fp))

    async with httpx.AsyncClient(headers={"User-Agent": "repo-converter-action/1.0"}) as client:
        for fp in files_to_process:
            await process_file(fp, instructions, None, client, str(source_dir))


def copy_tree_contents(src: Path, dest: Path):
    for item in src.iterdir():
        if item.name == ".git":
            continue
        target = dest / item.name
        if item.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def main():
    parser = argparse.ArgumentParser(description="Convert a source repository and publish to a destination repository.")
    parser.add_argument("--context-repo", required=True, help="HTTPS URL of source repository")
    parser.add_argument("--destination-repo", required=True, help="HTTPS URL of destination repository")
    parser.add_argument("--destination-stack", required=True, help="Target technology stack")
    parser.add_argument("--branch", default="main", help="Destination branch")
    parser.add_argument("--token", default=os.getenv("GITHUB_TOKEN"), help="GitHub token for destination push")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source_dir = tmp_path / "context_repo"
        dest_dir = tmp_path / "destination_repo"

        run(["git", "clone", "--depth", "1", normalize_repo_url(args.context_repo), str(source_dir)])
        run(["git", "clone", "--depth", "1", "--branch", args.branch, normalize_repo_url(args.destination_repo, args.token), str(dest_dir)])

        instructions = (
            f"Convert this repository to {args.destination_stack}. "
            "Return complete production-ready files with configs, tests, and docs as needed."
        )
        asyncio.run(convert_repo(source_dir, instructions))

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
