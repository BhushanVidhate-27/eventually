#!/usr/bin/env python3
"""Eventually — a slow, local-first tidier for a messy folder.

Eventually reads filenames only. It asks a local open-weight model where each
file belongs, prints a plan, and moves nothing until you pass --apply.

No API keys. No cloud. No file contents. Undo is one command.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

VERSION = "1.0.0"
DEFAULT_MODEL = "llama3.2:1b"
DEFAULT_ENDPOINT = "http://localhost:11434"
DEFAULT_STATE_DIR = Path.home() / ".eventually"

# Prefer a slightly larger model when the user already has one. The 1B model is
# the floor: small enough to run on a laptop, good enough for filing by name.
PREFERRED_MODELS = (
    "qwen2.5:3b",
    "llama3.2:3b",
    "llama3.2:1b",
    "gemma2:2b",
    "phi3:mini",
)

FOLDERS = (
    "Documents",
    "Spreadsheets",
    "Images",
    "Screenshots",
    "Installers",
    "Archives",
    "Audio",
    "Video",
    "Code",
    "Receipts",
    "To Sort",
)

FOLDER_GUIDE = """\
Documents = PDFs, Word docs, text notes, presentations, contracts, leases
Spreadsheets = xlsx, csv, budgets, financial tables
Images = photos, graphics, headshots, png, jpg, heic
Screenshots = screen captures, filenames starting Screenshot or Screen Shot
Installers = dmg, pkg, exe, appimage, setup files
Archives = zip, tar, 7z, rar, backups
Audio = mp3, wav, m4a, voice notes, music
Video = mp4, mov, mkv, clips, screen recordings
Code = py, js, ts, sh, json, yaml, source code, package manifests
Receipts = receipts, invoices, tax paperwork, bank statements
To Sort = unknown, ambiguous, or unsure"""

SYSTEM_PROMPT = f"""\
You are Eventually, a calm and careful filing assistant. Choose one folder for
the file based only on its filename.

Folders:
{FOLDER_GUIDE}

Examples:
Screenshot 2026-10-24 at 11.42.18.png -> Screenshots
invoice-october-acme.pdf -> Receipts
receipt-cafe-nero.jpg -> Receipts (a photo of a receipt is still a receipt)
main.py -> Code
Figma.dmg -> Installers
package-lock.json -> Code
lease-agreement-signed.pdf -> Documents
project-brief-final-FINAL.docx -> Documents
notes-for-monday.md -> Documents
README.md -> Code
unknown-file-7f3a.bin -> To Sort

Rules:
- Never invent a folder.
- A source-code extension (.py, .js, .ts, .sh) always means Code.
- An installer extension always means Installers.
- A photo or scan of a receipt, invoice or tax form goes in Receipts, even if
  it is a .jpg or .png.
- Screenshots go in Screenshots, not Images.
- Invoices, receipts, tax and statements go in Receipts, not Documents.
- If you are unsure, use To Sort.
- why: at most 8 words, and never repeat the filename.

Return JSON with exactly three keys: folder, confidence, why.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "folder": {"type": "string", "enum": list(FOLDERS)},
        "confidence": {"type": "number"},
        "why": {"type": "string"},
    },
    "required": ["folder", "confidence", "why"],
}

# Strong extensions get a safety rail. The model still decides; this only
# catches a tiny model doing something obviously silly (e.g. main.py ->
# Spreadsheets). The first value is the safe default, the set is what the model
# is allowed to choose without being overruled.
EXTENSION_RULES: dict[str, tuple[str, frozenset[str]]] = {
    ".py": ("Code", frozenset({"Code"})),
    ".js": ("Code", frozenset({"Code"})),
    ".jsx": ("Code", frozenset({"Code"})),
    ".ts": ("Code", frozenset({"Code"})),
    ".tsx": ("Code", frozenset({"Code"})),
    ".sh": ("Code", frozenset({"Code"})),
    ".rb": ("Code", frozenset({"Code"})),
    ".go": ("Code", frozenset({"Code"})),
    ".rs": ("Code", frozenset({"Code"})),
    ".java": ("Code", frozenset({"Code"})),
    ".json": ("Code", frozenset({"Code", "To Sort"})),
    ".yaml": ("Code", frozenset({"Code"})),
    ".yml": ("Code", frozenset({"Code"})),
    ".toml": ("Code", frozenset({"Code"})),
    ".dmg": ("Installers", frozenset({"Installers"})),
    ".pkg": ("Installers", frozenset({"Installers"})),
    ".exe": ("Installers", frozenset({"Installers"})),
    ".msi": ("Installers", frozenset({"Installers"})),
    ".appimage": ("Installers", frozenset({"Installers"})),
    ".zip": ("Archives", frozenset({"Archives"})),
    ".tar": ("Archives", frozenset({"Archives"})),
    ".tar.gz": ("Archives", frozenset({"Archives"})),
    ".tar.bz2": ("Archives", frozenset({"Archives"})),
    ".tar.xz": ("Archives", frozenset({"Archives"})),
    ".gz": ("Archives", frozenset({"Archives"})),
    ".7z": ("Archives", frozenset({"Archives"})),
    ".rar": ("Archives", frozenset({"Archives"})),
    ".mp3": ("Audio", frozenset({"Audio"})),
    ".wav": ("Audio", frozenset({"Audio"})),
    ".m4a": ("Audio", frozenset({"Audio"})),
    ".flac": ("Audio", frozenset({"Audio"})),
    ".aac": ("Audio", frozenset({"Audio"})),
    ".mp4": ("Video", frozenset({"Video"})),
    ".mov": ("Video", frozenset({"Video"})),
    ".mkv": ("Video", frozenset({"Video"})),
    ".webm": ("Video", frozenset({"Video"})),
    ".avi": ("Video", frozenset({"Video"})),
    ".png": ("Images", frozenset({"Images", "Screenshots", "Receipts"})),
    ".jpg": ("Images", frozenset({"Images", "Screenshots", "Receipts"})),
    ".jpeg": ("Images", frozenset({"Images", "Screenshots", "Receipts"})),
    ".heic": ("Images", frozenset({"Images", "Screenshots", "Receipts"})),
    ".heif": ("Images", frozenset({"Images", "Screenshots", "Receipts"})),
    ".gif": ("Images", frozenset({"Images", "Screenshots"})),
    ".webp": ("Images", frozenset({"Images", "Screenshots", "Receipts"})),
    ".svg": ("Images", frozenset({"Images"})),
    ".psd": ("Images", frozenset({"Images"})),
    ".xlsx": ("Spreadsheets", frozenset({"Spreadsheets"})),
    ".xls": ("Spreadsheets", frozenset({"Spreadsheets"})),
    ".csv": ("Spreadsheets", frozenset({"Spreadsheets"})),
    ".numbers": ("Spreadsheets", frozenset({"Spreadsheets"})),
    ".pdf": ("To Sort", frozenset({"Documents", "Receipts", "To Sort"})),
    ".doc": ("Documents", frozenset({"Documents"})),
    ".docx": ("Documents", frozenset({"Documents"})),
    ".pages": ("Documents", frozenset({"Documents"})),
    ".rtf": ("Documents", frozenset({"Documents"})),
    ".txt": ("Documents", frozenset({"Documents", "Code", "To Sort"})),
    ".md": ("Documents", frozenset({"Documents", "Code", "To Sort"})),
    ".pptx": ("Documents", frozenset({"Documents"})),
    ".key": ("Documents", frozenset({"Documents"})),
    ".epub": ("Documents", frozenset({"Documents"})),
    ".bin": ("To Sort", frozenset({"To Sort"})),
    ".iso": ("To Sort", frozenset({"Installers", "Archives", "To Sort"})),
}

RULE_REASONS = {
    ".py": "Python source file",
    ".js": "JavaScript source file",
    ".jsx": "JavaScript source file",
    ".ts": "TypeScript source file",
    ".tsx": "TypeScript source file",
    ".sh": "shell script",
    ".rb": "Ruby source file",
    ".go": "Go source file",
    ".rs": "Rust source file",
    ".java": "Java source file",
    ".json": "JSON data or manifest",
    ".yaml": "YAML config",
    ".yml": "YAML config",
    ".toml": "TOML config",
    ".dmg": "macOS installer",
    ".pkg": "macOS installer",
    ".exe": "Windows installer",
    ".msi": "Windows installer",
    ".appimage": "Linux app image",
    ".zip": "zip archive",
    ".tar": "tar archive",
    ".tar.gz": "compressed archive",
    ".tar.bz2": "compressed archive",
    ".tar.xz": "compressed archive",
    ".gz": "compressed archive",
    ".7z": "7z archive",
    ".rar": "rar archive",
    ".mp3": "audio file",
    ".wav": "audio file",
    ".m4a": "audio file",
    ".flac": "audio file",
    ".aac": "audio file",
    ".mp4": "video file",
    ".mov": "video file",
    ".mkv": "video file",
    ".webm": "video file",
    ".avi": "video file",
    ".png": "image file",
    ".jpg": "image file",
    ".jpeg": "image file",
    ".heic": "image file",
    ".heif": "image file",
    ".gif": "image file",
    ".webp": "image file",
    ".svg": "image file",
    ".psd": "image file",
    ".xlsx": "spreadsheet",
    ".xls": "spreadsheet",
    ".csv": "spreadsheet",
    ".numbers": "spreadsheet",
    ".pdf": "PDF needs a closer look",
    ".doc": "word document",
    ".docx": "word document",
    ".pages": "pages document",
    ".rtf": "rich text document",
    ".txt": "text note",
    ".md": "markdown note",
    ".pptx": "presentation",
    ".key": "presentation",
    ".epub": "ebook",
    ".bin": "unknown binary",
    ".iso": "disk image",
}

SKIP_SUFFIXES = frozenset({".crdownload", ".part", ".download", ".tmp"})
SKIP_NAMES = frozenset({"Thumbs.db", "desktop.ini"})

DEMO_FILES = (
    "Screenshot 2026-10-24 at 11.42.18.png",
    "invoice-october-acme.pdf",
    "tax-2025-final-v2.pdf",
    "IMG_4821.HEIC",
    "Figma.dmg",
    "project-brief-final-FINAL.docx",
    "q3-budget.xlsx",
    "receipt-cafe-nero.jpg",
    "voice-note-mom.m4a",
    "holiday-clip.mp4",
    "backup-2026.zip",
    "main.py",
    "package-lock.json",
    "lease-agreement-signed.pdf",
    "unknown-file-7f3a.bin",
    "headshot-bhushan.png",
    "meeting-transcript.txt",
    "design-review.mov",
    "notes-for-monday.md",
    "spotify-playlist.csv",
)


class ModelError(RuntimeError):
    """The local model could not be reached or did not answer usefully."""


@dataclass(frozen=True)
class FileInfo:
    name: str
    size: int


@dataclass(frozen=True)
class Decision:
    folder: str
    why: str
    confidence: float = 0.5
    source: str = "model"


@dataclass
class PlannedFile:
    name: str
    size: int
    folder: str
    why: str
    source: str


def file_suffix(name: str) -> str:
    lower = name.casefold()
    for compound in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if lower.endswith(compound):
            return compound
    return Path(lower).suffix


def list_target_files(target: Path) -> list[FileInfo]:
    """List loose files in one folder. Never recurse, never follow symlinks."""
    files: list[FileInfo] = []
    for entry in sorted(target.iterdir(), key=lambda p: p.name.casefold()):
        name = entry.name
        if name.startswith(".") or name.startswith("~$"):
            continue
        if name in SKIP_NAMES:
            continue
        if entry.is_symlink() or not entry.is_file():
            continue
        if file_suffix(name) in SKIP_SUFFIXES:
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        files.append(FileInfo(name=name, size=size))
    return files


def canonical_folder(value: str) -> str | None:
    wanted = value.strip().casefold()
    for folder in FOLDERS:
        if folder.casefold() == wanted:
            return folder
    return None


def clean_why(text: str) -> str:
    cleaned = " ".join(str(text).split())[:80]
    if not cleaned:
        return "no reason given"
    if cleaned.casefold() in {folder.casefold() for folder in FOLDERS}:
        return "filed by name"
    # A 1B model sometimes repeats a different filename as its reason. Keep
    # the audit log readable by replacing that with something honest.
    if re.search(
        r"\.(pdf|docx?|txt|md|png|jpe?g|heic|heif|dmg|pkg|zip|tar|gz|mp4|mov|"
        r"m4a|mp3|wav|xlsx?|csv|json|py|js|ts|bin)\b",
        cleaned,
        re.IGNORECASE,
    ):
        return "filed by name"
    return cleaned


def parse_decision(raw: str) -> Decision:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelError(f"model returned invalid JSON: {exc.msg}") from exc

    # Be forgiving if a model wraps a single decision in an items list.
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
        if not items:
            raise ModelError("model returned an empty items list")
        data = items[0]

    if not isinstance(data, dict):
        raise ModelError("model returned something other than a JSON object")

    folder = canonical_folder(str(data.get("folder", ""))) or "To Sort"
    why = clean_why(str(data.get("why", "")))
    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = min(1.0, max(0.0, confidence))
    return Decision(folder=folder, why=why, confidence=confidence)


def apply_guardrail(name: str, decision: Decision) -> Decision:
    lower = name.casefold()
    suffix = file_suffix(name)
    # A receipt photographed with a phone is still a receipt. The 1B model
    # sometimes sees .jpg first; this small rail keeps the intent.
    if suffix in {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".pdf"}:
        if any(word in lower for word in ("receipt", "invoice", "tax")):
            if decision.folder in {"Images", "Documents", "To Sort"}:
                return Decision(
                    folder="Receipts",
                    why="receipt or invoice",
                    confidence=1.0,
                    source="guardrail",
                )

    # Text notes are Documents unless they are clearly project documentation.
    if suffix in {".txt", ".md"} and any(
        word in lower for word in ("notes", "note", "meeting", "transcript", "journal", "todo")
    ):
        if decision.folder in {"To Sort", "Code"}:
            return Decision(
                folder="Documents",
                why="text note",
                confidence=1.0,
                source="guardrail",
            )

    rule = EXTENSION_RULES.get(suffix)
    if not rule:
        return decision
    default, allowed = rule
    if decision.folder in allowed:
        return decision
    reason = RULE_REASONS.get(suffix, f"{suffix} file")
    return Decision(
        folder=default,
        why=reason,
        confidence=1.0,
        source="guardrail",
    )


def post_json(url: str, payload: dict, timeout: float = 120) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:240]
        raise ModelError(f"Ollama HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ModelError(f"cannot reach Ollama: {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ModelError("Ollama returned invalid JSON") from exc


def ask_model(name: str, model: str, endpoint: str, timeout: float = 120) -> Decision:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Filename: {name}"},
        ],
        "stream": False,
        "format": RESPONSE_SCHEMA,
        "options": {"temperature": 0, "seed": 42, "num_predict": 120},
        "keep_alive": "10m",
    }
    data = post_json(endpoint.rstrip("/") + "/api/chat", payload, timeout=timeout)
    try:
        content = data["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise ModelError("Ollama returned an unexpected response") from exc
    return parse_decision(content)


def list_models(endpoint: str, timeout: float = 3) -> list[str] | None:
    """Return installed model names, or None if the server cannot be reached."""
    try:
        with urllib.request.urlopen(
            endpoint.rstrip("/") + "/api/tags", timeout=timeout
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    models = data.get("models", []) if isinstance(data, dict) else []
    return [
        str(item.get("name"))
        for item in models
        if isinstance(item, dict) and item.get("name")
    ]


def is_installed(name: str, installed: Sequence[str]) -> bool:
    if name in installed or f"{name}:latest" in installed:
        return True
    if ":" not in name:
        return any(model.startswith(name + ":") for model in installed)
    return False


def choose_model(requested: str | None, endpoint: str) -> str:
    installed = list_models(endpoint)
    if installed is None:
        raise ModelError(
            f"no local Ollama server answered at {endpoint}. "
            "Start it with `ollama serve`, then pull a model."
        )
    if not installed:
        raise ModelError(
            "Ollama is running, but it has no models yet. "
            f"Pull one with `ollama pull {requested or DEFAULT_MODEL}`."
        )
    if requested:
        if not is_installed(requested, installed):
            raise ModelError(
                f"model {requested!r} is not installed. "
                f"Run `ollama pull {requested}` first."
            )
        return requested
    for candidate in PREFERRED_MODELS:
        if is_installed(candidate, installed):
            return candidate
    return installed[0]


def short_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text[:100] or exc.__class__.__name__


def safe_decision(
    name: str,
    model: str,
    endpoint: str,
    ask: Callable[[str, str, str], Decision] = ask_model,
) -> Decision:
    try:
        decision = ask(name, model, endpoint)
    except Exception as exc:  # fail closed: never auto-file on a model error
        return Decision(
            folder="To Sort",
            why=f"model could not decide: {short_error(exc)}",
            confidence=0.0,
            source="error",
        )
    return apply_guardrail(name, decision)


def classify_files(
    files: Sequence[FileInfo],
    model: str,
    endpoint: str,
    workers: int = 4,
    ask: Callable[[str, str, str], Decision] = ask_model,
) -> list[PlannedFile]:
    decisions: dict[str, Decision] = {}
    if workers <= 1:
        for item in files:
            decisions[item.name] = safe_decision(item.name, model, endpoint, ask)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(safe_decision, item.name, model, endpoint, ask): item
                for item in files
            }
            for future in as_completed(futures):
                item = futures[future]
                decisions[item.name] = future.result()

    return [
        PlannedFile(
            name=item.name,
            size=item.size,
            folder=decisions[item.name].folder,
            why=decisions[item.name].why,
            source=decisions[item.name].source,
        )
        for item in files
    ]


def build_groups(planned: Sequence[PlannedFile]) -> dict[str, list[PlannedFile]]:
    groups: dict[str, list[PlannedFile]] = {folder: [] for folder in FOLDERS}
    for item in planned:
        groups[item.folder].append(item)
    for items in groups.values():
        items.sort(key=lambda item: item.name.casefold())
    return {folder: items for folder, items in groups.items() if items}


def split_name(name: str) -> tuple[str, str]:
    lower = name.casefold()
    for compound in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if lower.endswith(compound):
            return name[: -len(compound)], name[-len(compound) :]
    path = Path(name)
    return path.stem, path.suffix


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = split_name(path.name)
    for number in range(2, 10_000):
        candidate = path.with_name(f"{stem} ({number}){suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"could not find a free name for {path.name}")


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def fit(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + "…"


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        pass
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def write_manifest(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _color_enabled() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _style(text: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


def print_plan(
    target: Path,
    groups: dict[str, list[PlannedFile]],
    model: str,
    elapsed: float,
    applying: bool = False,
    explain: bool = False,
) -> None:
    color = _color_enabled()
    bold = lambda text: _style(text, "1", color)
    dim = lambda text: _style(text, "2", color)

    total_files = sum(len(items) for items in groups.values())
    total_bytes = sum(item.size for items in groups.values() for item in items)
    folder_count = len(groups)
    to_sort = len(groups.get("To Sort", []))
    errors = sum(
        1 for items in groups.values() for item in items if item.source == "error"
    )

    print()
    print(bold(f"Eventually {VERSION}") + dim(" — your folder, eventually."))
    print()
    print(
        f"Looking at {total_files} names in {display_path(target)}. "
        "No file contents are read."
    )
    print(f"Asking {bold(model)}, running locally. Nothing moves yet.")
    print()

    for folder, items in groups.items():
        print(bold(f"  {folder}") + dim(f" ({len(items)})"))
        for item in items:
            line = f"    {item.name}"
            if explain and item.why:
                line += dim(f"  ·  {fit(item.why, 60)}")
            print(line)
        print()

    moved = "0 files moved yet" if applying else "0 files moved"
    print(
        dim(
            f"{total_files} files ({format_bytes(total_bytes)}) → "
            f"{folder_count} folders · {elapsed:.1f}s · "
            f"0 bytes uploaded · {moved}."
        )
    )

    if to_sort:
        print(
            f"\n{to_sort} file(s) are in To Sort. That is allowed. "
            "Not every download needs a decision today."
        )
    if errors:
        print(
            f"{errors} file(s) hit a model error and stayed in To Sort. "
            "Nothing was guessed."
        )

    if applying:
        print("\nApplying this plan now. A manifest is written so you can undo.")
    else:
        command = f"python3 eventually.py {shlex.quote(str(target))} --apply"
        print("\nThis is only a plan. To move the files:")
        print(f"  {command}")
        print("\nUndo a previous run:")
        print("  python3 eventually.py --undo")


def apply_groups(
    target: Path,
    groups: dict[str, list[PlannedFile]],
    state_dir: Path,
) -> int:
    started = time.perf_counter()
    manifest: dict = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": str(target),
        "moves": [],
        "created_dirs": [],
        "errors": [],
    }
    manifest_path = state_dir / "last-run.json"
    moved = 0
    errors: list[str] = []
    created_dirs: list[Path] = []

    try:
        for folder, items in groups.items():
            destination_dir = target / folder
            if not destination_dir.exists():
                try:
                    destination_dir.mkdir(parents=True)
                    created_dirs.append(destination_dir)
                except OSError as exc:
                    errors.append(f"could not create {destination_dir}: {exc}")
                    continue

            for item in items:
                source = target / item.name
                if not source.exists() or not source.is_file():
                    errors.append(f"{item.name}: no longer there")
                    continue
                try:
                    destination = unique_path(destination_dir / item.name)
                    shutil.move(str(source), str(destination))
                except OSError as exc:
                    errors.append(f"{item.name}: {exc}")
                    continue
                moved += 1
                manifest["moves"].append(
                    {
                        "from": str(source),
                        "to": str(destination),
                        "folder": folder,
                        "why": item.why,
                    }
                )
    except KeyboardInterrupt:
        manifest["errors"] = errors
        manifest["created_dirs"] = [str(path) for path in created_dirs]
        if manifest["moves"]:
            write_manifest(manifest_path, manifest)
        print("\nStopped early. Files already moved can be undone with --undo.")
        return 130

    manifest["errors"] = errors
    manifest["created_dirs"] = [str(path) for path in created_dirs]
    if manifest["moves"]:
        write_manifest(manifest_path, manifest)

    elapsed = time.perf_counter() - started
    color = _color_enabled()
    bold = lambda text: _style(text, "1", color)

    print()
    print(
        bold(
            f"Moved {moved} file{'s' if moved != 1 else ''} into "
            f"{len(groups)} folders in {elapsed:.1f}s."
        )
    )
    if errors:
        print(f"{len(errors)} file(s) stayed put:")
        for error in errors[:5]:
            print(f"  {error}")
        if len(errors) > 5:
            print(f"  …and {len(errors) - 5} more.")
    if manifest["moves"]:
        print(f"Manifest: {display_path(manifest_path)}")
        print("Undo: python3 eventually.py --undo")
    return 1 if errors else 0


def undo_last_run(state_dir: Path) -> int:
    manifest_path = state_dir / "last-run.json"
    if not manifest_path.exists():
        print("No last run to undo. Nothing to do.")
        return 0
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read {display_path(manifest_path)}: {exc}")
        return 1

    moves = data.get("moves", [])
    if not isinstance(moves, list) or not moves:
        print("The last run moved nothing. Nothing to undo.")
        manifest_path.unlink(missing_ok=True)
        return 0

    started = time.perf_counter()
    restored = 0
    conflicts: list[str] = []
    remaining: list[dict] = []

    for move in reversed(moves):
        try:
            source = Path(move["from"])
            destination = Path(move["to"])
        except (KeyError, TypeError):
            conflicts.append("a malformed manifest entry")
            continue

        if not destination.exists():
            conflicts.append(f"{destination.name}: missing from its folder")
            continue
        if source.exists():
            conflicts.append(f"{source.name}: original path is occupied")
            remaining.append(move)
            continue
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(source))
        except OSError as exc:
            conflicts.append(f"{source.name}: {exc}")
            remaining.append(move)
            continue
        restored += 1

    for directory in reversed(data.get("created_dirs", [])):
        try:
            Path(directory).rmdir()
        except OSError:
            pass

    elapsed = time.perf_counter() - started
    if conflicts:
        data["moves"] = remaining
        data["created_dirs"] = []
        write_manifest(manifest_path, data)
        print(
            f"Restored {restored} of {len(moves)} files in {elapsed:.1f}s. "
            f"{len(conflicts)} could not be restored:"
        )
        for conflict in conflicts[:5]:
            print(f"  {conflict}")
        if len(conflicts) > 5:
            print(f"  …and {len(conflicts) - 5} more.")
        print(f"The remaining manifest is {display_path(manifest_path)}.")
        return 1

    manifest_path.unlink(missing_ok=True)
    print(
        f"Restored {restored} files in {elapsed:.1f}s. "
        "The folder is messy again, on purpose."
    )
    return 0


def create_demo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in DEMO_FILES:
        path = root / name
        if not path.exists():
            path.write_text(f"demo placeholder for {name}\n", encoding="utf-8")
    return root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eventually",
        description=(
            "A slow, local-first tidier. Reads filenames only, asks a local "
            "open-weight model for a plan, and moves nothing until --apply."
        ),
        epilog=(
            "Examples:\n"
            "  python3 eventually.py --demo\n"
            "  python3 eventually.py ~/Downloads\n"
            "  python3 eventually.py ~/Downloads --apply\n"
            "  python3 eventually.py --undo"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="folder to tidy (default: ~/Downloads)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="move the files after printing the plan",
    )
    parser.add_argument(
        "--undo",
        action="store_true",
        help="undo the most recent --apply",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="create and use ./demo-downloads with 20 sample filenames",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="show the model's one-line reason for each file",
    )
    parser.add_argument(
        "--model",
        help=f"Ollama model name (default: auto, e.g. {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"Ollama endpoint (default: {DEFAULT_ENDPOINT})",
    )
    parser.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help=f"where the undo manifest lives (default: {DEFAULT_STATE_DIR})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--version", action="version", version=f"Eventually {VERSION}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.undo and args.apply:
        parser.error("--undo and --apply cannot be used together")

    state_dir = Path(args.state_dir).expanduser()
    if args.undo:
        return undo_last_run(state_dir)

    if args.demo:
        target = Path(args.target).expanduser() if args.target else Path("demo-downloads")
        create_demo(target)
        print(f"Demo folder ready: {display_path(target.resolve())}")
    else:
        target = Path(args.target).expanduser() if args.target else Path.home() / "Downloads"

    target = target.resolve()
    if not target.exists():
        parser.error(f"folder not found: {target}")
    if not target.is_dir():
        parser.error(f"not a folder: {target}")
    if target == Path.home() or target == Path("/"):
        parser.error(
            "that folder is too broad. Point Eventually at Downloads, Desktop, "
            "or another single folder."
        )

    files = list_target_files(target)
    if not files:
        print(f"No loose files in {display_path(target)}. Go take a walk.")
        return 0

    try:
        model = choose_model(args.model, args.endpoint)
    except ModelError as exc:
        print("Eventually needs a local Ollama model.\n", file=sys.stderr)
        print(f"{exc}\n", file=sys.stderr)
        print("If the server is not running:", file=sys.stderr)
        print("  ollama serve", file=sys.stderr)
        print("\nThen pull a small open-weight model:", file=sys.stderr)
        print(f"  ollama pull {args.model or DEFAULT_MODEL}", file=sys.stderr)
        return 2

    started = time.perf_counter()
    try:
        planned = classify_files(
            files,
            model=model,
            endpoint=args.endpoint,
            workers=max(1, args.workers),
        )
    except KeyboardInterrupt:
        print("\nStopped. Nothing moved.")
        return 130
    elapsed = time.perf_counter() - started
    groups = build_groups(planned)

    print_plan(
        target,
        groups,
        model,
        elapsed,
        applying=args.apply,
        explain=args.explain,
    )
    if args.apply:
        return apply_groups(target, groups, state_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
