"""
Live editing + re-indexing of a tenant's knowledge documents (the .txt files
the RAG chatbot answers from), used by admin.py's "Knowledge base" page.

WHY THIS RUNS INSIDE THE SERVER PROCESS
Running ingestion/build_index.py as a separate process while the bot is up
leaves the bot holding a stale ChromaDB handle (that's what caused the
"Error creating hnsw segment reader: Nothing found on disk" 500s). Here the
re-index uses the SAME chromadb client and embedding model that rag.py
already loaded, so there is nothing stale to restart — and no second copy of
the embedding model in memory either.

HOW A SAVE UPDATES THE LIVE BOT
1. The new text is written to documents/<tenant>/<file>.txt (the previous
   version is copied to documents_backups/<tenant>/ first).
2. A background thread re-chunks the folder with the SAME loader that
   build_index.py uses, embeds the chunks, and upserts them into the
   tenant's collection, then deletes chunks that no longer exist.
3. Embedding happens BEFORE anything is written to Chroma, so if it fails
   the bot keeps answering from the previous content untouched. An empty
   result is refused outright so the knowledge base can never be wiped.

LIMITS (read before relying on this)
- Status/locking is in-memory and per process: it assumes the app runs as a
  single uvicorn process (no --workers N).
- Only .txt files are editable here. Any other files in the tenant's folder
  (e.g. PDFs) are still indexed by the rebuild, just not editable.
- To make another tenant editable, add it to EDITABLE_TENANTS below.
"""

import importlib.util
import os
import re
import shutil
import sys
import threading
import traceback
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOCUMENTS_DIR = os.path.join(BASE_DIR, "documents")
BACKUPS_DIR = os.path.join(BASE_DIR, "documents_backups")
INGESTION_DIR = os.path.join(BASE_DIR, "ingestion")

# tenant -> sub-folder of documents/ that holds that tenant's files.
# Mirrors "documents_subdir" in ingestion/build_index.py.
EDITABLE_TENANTS = {"dominos": "dominos"}

MAX_DOCUMENT_CHARS = 200_000
MAX_BACKUPS_PER_FILE = 30

_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.()\-]{0,80}\.txt$")
_STAMP_FMT = "%Y%m%d-%H%M%S"


# ---------------------------------------------------------------------------
# Document files
# ---------------------------------------------------------------------------

def is_editable(tenant: str) -> bool:
    return tenant in EDITABLE_TENANTS


def document_dir(tenant: str) -> str:
    return os.path.join(DOCUMENTS_DIR, EDITABLE_TENANTS[tenant])


def backup_dir(tenant: str) -> str:
    return os.path.join(BACKUPS_DIR, tenant)


def clean_filename(name):
    """Returns the file name if it is safe to use (plain .txt name, no path
    parts), else None. Everything that touches the disk goes through this."""
    name = (name or "").strip()
    return name if _FILENAME_RE.match(name) else None


def list_documents(tenant: str) -> list:
    folder = document_dir(tenant)
    if not os.path.isdir(folder):
        return []
    return sorted(
        f for f in os.listdir(folder)
        if _FILENAME_RE.match(f) and os.path.isfile(os.path.join(folder, f))
    )


def read_document(tenant: str, filename: str) -> str:
    path = os.path.join(document_dir(tenant), filename)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def save_document(tenant: str, filename: str, text: str) -> bool:
    """Writes the document (backing up the previous version first).
    Returns False if the text is identical to what's already saved."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    folder = document_dir(tenant)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, filename)

    if os.path.isfile(path):
        if read_document(tenant, filename) == text:
            return False
        _backup(tenant, filename, path)

    # Write to a temp file OUTSIDE the documents folder, then swap it in, so
    # a rebuild can never see a half-written document.
    os.makedirs(backup_dir(tenant), exist_ok=True)
    tmp_path = os.path.join(backup_dir(tenant), f".tmp-{filename}")
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp_path, path)
    return True


# ---------------------------------------------------------------------------
# Version history
# ---------------------------------------------------------------------------

def _backup(tenant: str, filename: str, path: str):
    folder = backup_dir(tenant)
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime(_STAMP_FMT)
    shutil.copy2(path, os.path.join(folder, f"{filename}.{stamp}.bak"))

    # Keep only the newest MAX_BACKUPS_PER_FILE versions of this file.
    for old in list_backups(tenant, filename)[MAX_BACKUPS_PER_FILE:]:
        try:
            os.remove(os.path.join(folder, old["id"]))
        except OSError:
            pass


def list_backups(tenant: str, filename: str) -> list:
    """Newest first: [{"id": <backup file name>, "label": "11 Oct 2026, 10:42 PM"}]"""
    folder = backup_dir(tenant)
    if not os.path.isdir(folder):
        return []
    pattern = re.compile(rf"^{re.escape(filename)}\.(\d{{8}}-\d{{6}})\.bak$")
    found = []
    for f in os.listdir(folder):
        m = pattern.match(f)
        if not m:
            continue
        try:
            label = datetime.strptime(m.group(1), _STAMP_FMT).strftime("%d %b %Y, %I:%M:%S %p")
        except ValueError:
            continue
        found.append({"id": f, "stamp": m.group(1), "label": label})
    found.sort(key=lambda b: b["stamp"], reverse=True)
    return found


def read_backup(tenant: str, filename: str, backup_id: str):
    """Returns (text, label) for a backup, or None. The id must be one that
    list_backups() actually returned — user input never becomes a path."""
    for b in list_backups(tenant, filename):
        if b["id"] == backup_id:
            with open(os.path.join(backup_dir(tenant), b["id"]), "r", encoding="utf-8", errors="replace") as f:
                return f.read(), b["label"]
    return None


# ---------------------------------------------------------------------------
# Live re-index
# ---------------------------------------------------------------------------

_status = {}
_lock = threading.Lock()


def _now_label() -> str:
    return datetime.now().strftime("%I:%M %p").lstrip("0")


def get_status(tenant: str) -> dict:
    with _lock:
        return dict(_status.get(tenant, {"state": "idle", "message": ""}))


def is_running(tenant: str) -> bool:
    return get_status(tenant)["state"] == "running"


def start_reindex(tenant: str) -> bool:
    """Starts a background re-index. Returns False if one is already running."""
    with _lock:
        if _status.get(tenant, {}).get("state") == "running":
            return False
        _status[tenant] = {
            "state": "running",
            "message": "Updating the bot with your changes. This takes about a minute; the bot keeps answering from the old content until it finishes.",
        }
    threading.Thread(target=_run_reindex, args=(tenant,), daemon=True).start()
    return True


def _load_chunker():
    """Loads chunk_documents from ingestion/loader.py — the exact function
    build_index.py uses — so chunking is identical to a manual rebuild."""
    if INGESTION_DIR not in sys.path:
        sys.path.append(INGESTION_DIR)  # in case loader.py imports siblings
    spec = importlib.util.spec_from_file_location(
        "ingestion_loader", os.path.join(INGESTION_DIR, "loader.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.chunk_documents


def _set_status(tenant: str, **fields):
    with _lock:
        _status[tenant] = fields


def _run_reindex(tenant: str):
    stage = "preparing"
    try:
        import rag  # already loaded by main.py; this reuses its client + model

        collection_name = rag.TENANT_CONFIGS[tenant]["collection_name"]
        chunks = _load_chunker()(document_dir(tenant))
        if not chunks:
            raise RuntimeError("no text could be read from the documents")

        stage = "embedding"
        texts = [c["text"] for c in chunks]
        embeddings = rag.model.encode(texts, show_progress_bar=False)

        stage = "writing"
        collection = rag.client.get_or_create_collection(collection_name)
        new_ids = [c["id"] for c in chunks]
        collection.upsert(
            ids=new_ids,
            embeddings=[e.tolist() for e in embeddings],
            documents=texts,
            metadatas=[
                {
                    "source": c["source"],
                    "page": c["page"],
                    "chunk_number": c["chunk_number"],
                }
                for c in chunks
            ],
        )

        keep = set(new_ids)
        stale = [i for i in collection.get(include=[])["ids"] if i not in keep]
        if stale:
            collection.delete(ids=stale)

        _set_status(
            tenant,
            state="done",
            chunks=len(chunks),
            message=f"Done at {_now_label()}. The bot is now answering from the updated content ({len(chunks)} sections).",
        )
        print(f"[reindex] {tenant}: indexed {len(chunks)} chunks, removed {len(stale)} stale")

    except Exception as e:
        traceback.print_exc()
        if stage == "writing":
            detail = "The update may be partly applied. Click \"Rebuild bot knowledge\" to try again."
        else:
            detail = "The bot is still answering from the previous content."
        _set_status(tenant, state="error", message=f"The update failed ({e}). {detail}")