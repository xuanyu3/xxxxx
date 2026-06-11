#!/usr/bin/env python3
"""
get_skill_bom.py — extract a Bill of Materials from an AI agent skill directory.

Pipeline: skill path -> walk files -> fill prompt template -> call LLM -> JSON BOM.

Usage:
    python get_skill_bom.py <skill_path> [--output OUT.json] [--model MODEL] [--prompt PROMPT.md]

Env:
    OPENAI_API_KEY       required, API key for the LLM
    OPENAI_BASE_URL      optional, default https://api.openai.com/v1
    SKILLBOM_MODEL       optional, default gpt-4o-mini (overridden by --model)
"""

import argparse
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path


TEXT_EXTS = {
    ".md", ".txt", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".html", ".htm", ".css", ".scss", ".xml", ".svg",
    ".go", ".rs", ".java", ".kt", ".c", ".cc", ".cpp", ".h", ".hpp", ".rb", ".php", ".lua",
    ".sql", ".graphql", ".proto",
    ".dockerfile", ".makefile", ".mk",
}
TEXT_NAMES = {
    "dockerfile", "makefile", "readme", "license", "notice", "authors",
    ".env.example", ".env.sample", ".dockerignore", ".gitignore", ".gitattributes",
}
SKIP_DIRS = {".git", ".hg", "node_modules", "__pycache__", ".venv", "venv",
             "dist", "build", ".cache", ".pytest_cache", ".mypy_cache", ".idea", ".vscode"}

PER_FILE_MAX_CHARS = 8000
TOTAL_CONTEXT_MAX_CHARS = 200_000

# Baked-in defaults so the script runs with zero flags / env vars.
# CLI args and env vars still override these. Replace with your own if forking.
DEFAULT_API_KEY  = "sk-A4klXRZzeEMcI7lmHybMd3sIZkrm7h0jqMWcAVm4d5J0kcNO"
DEFAULT_BASE_URL = "https://yunwu.ai/v1"
DEFAULT_MODEL    = "claude-sonnet-4-6"
DEFAULT_JSON_MODE = False  # yunwu's Claude rejects OpenAI's response_format=json_object


def is_text_file(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXTS:
        return True
    if path.name.lower() in TEXT_NAMES:
        return True
    try:
        with open(path, "rb") as f:
            blob = f.read(2048)
        if not blob:
            return True
        if b"\x00" in blob:
            return False
        blob.decode("utf-8")
        return True
    except Exception:
        return False


def walk_skill(skill_path: Path):
    files = []
    for root, dirs, names in os.walk(skill_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            fp = Path(root) / name
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            sha = hashlib.sha256()
            try:
                with open(fp, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        sha.update(chunk)
            except OSError:
                continue
            rel = fp.relative_to(skill_path).as_posix()
            files.append({
                "path": rel,
                "size": size,
                "sha256": sha.hexdigest(),
                "is_text": is_text_file(fp),
                "_abs": fp,
            })
    files.sort(key=lambda x: x["path"])
    return files


def build_context(files, total_max=TOTAL_CONTEXT_MAX_CHARS):
    tree_lines = []
    for f in files:
        kind = "text" if f["is_text"] else "binary"
        tree_lines.append(f"{f['path']}  ({f['size']} B, sha={f['sha256'][:12]}, {kind})")
    tree_str = "\n".join(tree_lines)

    content_blocks = []
    used = 0
    for f in files:
        if not f["is_text"]:
            continue
        try:
            text = f["_abs"].read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        truncated = False
        if len(text) > PER_FILE_MAX_CHARS:
            text = text[:PER_FILE_MAX_CHARS]
            truncated = True

        numbered = "\n".join(
            f"{i + 1:4d}: {line}" for i, line in enumerate(text.splitlines())
        )
        block = f"--- {f['path']} ---\n{numbered}\n"
        if truncated:
            block += f"[TRUNCATED at {PER_FILE_MAX_CHARS} chars; full file is {f['size']} bytes]\n"

        if used + len(block) > total_max:
            remaining = total_max - used
            if remaining > 200:
                content_blocks.append(block[:remaining] + "\n[CUT]\n")
            content_blocks.append(
                f"--- [remaining {len([1 for x in files if x['is_text']]) - len(content_blocks)} text files omitted: context budget exhausted] ---\n"
            )
            break

        content_blocks.append(block)
        used += len(block)

    return tree_str, "\n".join(content_blocks)


def call_llm(api_key: str, base_url: str, model: str, system: str, user: str,
             want_json: bool = True, timeout: int = 600) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
    }
    if want_json:
        body["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    max_attempts = 4
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            # 4xx is a client problem (bad key, bad model name) - don't retry
            if 400 <= e.code < 500:
                raise SystemExit(f"LLM HTTP {e.code}: {err_body}")
            last_err = f"HTTP {e.code}: {err_body[:300]}"
        except (urllib.error.URLError, ssl.SSLError, ConnectionError, TimeoutError) as e:
            last_err = f"{type(e).__name__}: {getattr(e, 'reason', e)}"

        if attempt < max_attempts:
            wait = 2 ** (attempt - 1)  # 1s, 2s, 4s
            print(f"[llm]  attempt {attempt}/{max_attempts} failed ({last_err}); retrying in {wait}s...",
                  file=sys.stderr)
            time.sleep(wait)
    else:
        raise SystemExit(f"LLM connection failed after {max_attempts} attempts. Last error: {last_err}\n"
                         f"  Check: (1) yunwu.ai is reachable, (2) API key valid, (3) try again in a minute.")

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise SystemExit(f"Unexpected LLM response shape: {json.dumps(data)[:500]}")


def extract_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        lines = [ln for ln in lines if not ln.startswith("```")]
        raw = "\n".join(lines).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("skill_path", type=Path, help="path to the skill directory")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="output JSON path (default: ./<skill_name>_bom.json)")
    parser.add_argument("--prompt", type=Path,
                        default=Path(__file__).parent / "get_skill_bom_prompt.md",
                        help="prompt template MD file")
    parser.add_argument("--model", default=os.environ.get("SKILLBOM_MODEL", DEFAULT_MODEL),
                        help=f"LLM model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", DEFAULT_API_KEY))
    parser.add_argument("--base-url",
                        default=os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL),
                        help=f"OpenAI-compatible endpoint base URL (default: {DEFAULT_BASE_URL})")
    json_group = parser.add_mutually_exclusive_group()
    json_group.add_argument("--no-json-mode", dest="json_mode", action="store_false",
                            help="do NOT request response_format=json_object")
    json_group.add_argument("--json-mode", dest="json_mode", action="store_true",
                            help="request response_format=json_object (only for endpoints that support it)")
    parser.set_defaults(json_mode=DEFAULT_JSON_MODE)
    parser.add_argument("--quiet", action="store_true", help="do not print BOM to stdout")
    args = parser.parse_args()

    skill_path = args.skill_path.resolve()
    if not skill_path.is_dir():
        sys.exit(f"skill path is not a directory: {skill_path}")
    if not args.prompt.exists():
        sys.exit(f"prompt file not found: {args.prompt}")
    if not args.api_key:
        sys.exit("no API key: set OPENAI_API_KEY env var, pass --api-key, or set DEFAULT_API_KEY in the script")

    prompt_template = args.prompt.read_text(encoding="utf-8")

    files = walk_skill(skill_path)
    if not files:
        sys.exit(f"no files found under {skill_path}")
    total_bytes = sum(f["size"] for f in files)
    text_count = sum(1 for f in files if f["is_text"])
    print(f"[walk] {len(files)} files ({text_count} text, {total_bytes:,} B) under {skill_path}",
          file=sys.stderr)

    tree, contents = build_context(files)
    print(f"[ctx]  tree={len(tree):,} chars  contents={len(contents):,} chars",
          file=sys.stderr)

    user_message = (
        prompt_template
        .replace("{{SKILL_NAME}}", skill_path.name)
        .replace("{{SKILL_PATH}}", str(skill_path))
        .replace("{{SKILL_TREE}}", tree)
        .replace("{{SKILL_FILES}}", contents)
    )

    print(f"[llm]  endpoint={args.base_url}  model={args.model}", file=sys.stderr)
    raw = call_llm(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        system=("You are a static analyzer that extracts Bills of Materials from AI agent skills. "
                "Follow the user's schema exactly. Output one JSON object and nothing else."),
        user=user_message,
        want_json=args.json_mode,
    )

    try:
        bom = extract_json(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"LLM returned non-JSON: {e}\n--- raw response ---\n{raw[:2000]}")

    bom["_meta"] = {
        "skill_path": str(skill_path),
        "skill_name": skill_path.name,
        "file_count": len(files),
        "text_file_count": text_count,
        "total_size_bytes": total_bytes,
        "model": args.model,
        "endpoint": args.base_url,
        "file_inventory": [
            {"path": f["path"], "size": f["size"], "sha256": f["sha256"], "is_text": f["is_text"]}
            for f in files
        ],
    }

    out_path = args.output or (Path.cwd() / f"{skill_path.name}_bom.json")
    out_path.write_text(json.dumps(bom, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[done] BOM written -> {out_path}", file=sys.stderr)

    if not args.quiet:
        print(json.dumps(bom, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
