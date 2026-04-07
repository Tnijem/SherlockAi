#!/usr/bin/env python3
"""Standalone NAS embedding worker — runs outside uvicorn to avoid GIL contention.

Reads already-extracted text from nas_text table, chunks it, embeds via Ollama,
and upserts to ChromaDB. Communicates status via JSON file.
"""
import os, sys, time, sqlite3, json, logging, hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("embed_worker")

STATUS_FILE = os.path.join(os.path.dirname(__file__), '..', 'data', 'embed_status.json')
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'sherlock.db')
MIN_CHARS = 100
BATCH_SIZE = 90000

def write_status(status):
    try:
        with open(STATUS_FILE, 'w') as f:
            json.dump(status, f)
    except Exception:
        pass

def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn

def get_pending(conn, limit=BATCH_SIZE):
    """Get files with text that aren't yet embedded (no nas_indexed marker)."""
    cur = conn.cursor()
    # Use a tracking table to know what's already embedded
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nas_embedded (
            file_path TEXT PRIMARY KEY,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            embedded_at TEXT NOT NULL
        )
    """)
    conn.commit()

    cur.execute("""
        SELECT t.file_path, c.filename, c.client_folder, c.category, t.text_content
        FROM nas_text t
        JOIN nas_catalog c ON c.file_path = t.file_path
        LEFT JOIN nas_embedded e ON e.file_path = t.file_path
        WHERE t.status = 'ok'
          AND t.char_count >= ?
          AND t.char_count <= 500000
          AND e.file_path IS NULL
          AND c.filename NOT LIKE '~$%%'
        ORDER BY
            CASE
                WHEN c.extension IN ('.docx','.doc','.pdf') THEN 1
                WHEN c.extension IN ('.xlsx','.xls','.csv') THEN 2
                ELSE 3
            END,
            t.char_count ASC
        LIMIT ?
    """, (MIN_CHARS, limit))
    return cur.fetchall()


def chunk_text(text, max_tokens=256, overlap=50):
    """Split text into overlapping chunks."""
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk_words = words[i:i + max_tokens]
        chunk = ' '.join(chunk_words)
        if len(chunk.strip()) >= MIN_CHARS:
            chunks.append(chunk)
        i += max_tokens - overlap
    return chunks if chunks else ([text] if len(text.strip()) >= MIN_CHARS else [])


def embed_text(text, ollama_url, model, max_retries=3):
    """Get embedding vector from Ollama with retry logic."""
    import urllib.request, math
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                f"{ollama_url}/api/embeddings",
                data=json.dumps({"model": model, "prompt": text[:2048]}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
            emb = data["embedding"]
            norm = math.sqrt(sum(x * x for x in emb)) or 1.0
            return [x / norm for x in emb]
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))  # backoff: 1s, 2s
            else:
                raise


def main():
    # Load config
    conf_path = os.path.join(os.path.dirname(__file__), '..', 'sherlock.conf')
    config = {}
    if os.path.exists(conf_path):
        with open(conf_path) as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    config[k.strip()] = v.strip()

    ollama_url = config.get('OLLAMA_URL', 'http://localhost:11434')
    embed_model = config.get('EMBED_MODEL', 'mxbai-embed-large')
    chroma_url = config.get('CHROMA_URL', 'http://localhost:8000')

    log.info("Connecting to ChromaDB at %s", chroma_url)

    import chromadb
    client = chromadb.HttpClient(host=chroma_url.replace('http://', '').split(':')[0],
                                  port=int(chroma_url.split(':')[-1]))
    collection = client.get_or_create_collection(
        name="sherlock_global",
        metadata={"hnsw:space": "cosine"},
    )

    existing_count = collection.count()
    log.info("ChromaDB collection 'sherlock_global' has %d existing vectors", existing_count)

    conn = get_conn()
    pending = get_pending(conn)
    total = len(pending)
    log.info("Embedding worker started: %d files to embed", total)

    status = {
        "active": True,
        "total_queued": total,
        "processed": 0,
        "embedded_ok": 0,
        "skipped": 0,
        "errors": 0,
        "chunks_added": 0,
        "current_file": "",
        "stage": "embedding",
        "started_at": time.time(),
        "pid": os.getpid(),
        "elapsed_s": 0,
    }
    write_status(status)

    for i, (file_path, filename, client_folder, category, text_content) in enumerate(pending):
        status["processed"] = i + 1
        status["current_file"] = filename
        status["elapsed_s"] = round(time.time() - status["started_at"])
        if i % 5 == 0:
            write_status(status)

        try:
            if not text_content or len(text_content.strip()) < MIN_CHARS:
                status["skipped"] += 1
                continue

            chunks = chunk_text(text_content)[:50]  # cap at 50 chunks per file
            if not chunks:
                status["skipped"] += 1
                continue

            fp_hash = hashlib.md5(file_path.encode()).hexdigest()[:12]
            ids = []
            documents = []
            embeddings = []
            metadatas = []

            for ci, chunk in enumerate(chunks):
                chunk_id = f"nas_{fp_hash}_{ci}"
                try:
                    embedding = embed_text(chunk, ollama_url, embed_model)
                except Exception as e:
                    err_body = ""
                    if hasattr(e, 'read'):
                        try: err_body = " | " + e.read().decode()[:100]
                        except: pass
                    log.warning("embed_call_failed: %s chunk %d: %s%s", filename, ci, str(e)[:100], err_body)
                    continue
                time.sleep(0.05)  # 50ms throttle between embed calls

                ids.append(chunk_id)
                documents.append(chunk)
                embeddings.append(embedding)
                metadatas.append({
                    "source": filename,
                    "path": file_path,
                    "chunk": ci,
                    "total_chunks": len(chunks),
                    "client_folder": client_folder or "",
                    "category": category or "",
                    "nas_indexed": "true",
                })

            if ids:
                # Upsert in batches of 100 to ChromaDB
                for b in range(0, len(ids), 100):
                    collection.upsert(
                        ids=ids[b:b+100],
                        documents=documents[b:b+100],
                        embeddings=embeddings[b:b+100],
                        metadatas=metadatas[b:b+100],
                    )

                # Track in SQLite
                conn.execute(
                    "INSERT OR REPLACE INTO nas_embedded (file_path, chunk_count, embedded_at) VALUES (?, ?, datetime('now'))",
                    (file_path, len(ids))
                )
                conn.commit()

                status["embedded_ok"] += 1
                status["chunks_added"] += len(ids)
            else:
                status["skipped"] += 1

            if (i + 1) % 50 == 0:
                log.info("embed_progress: %d/%d (ok=%d, chunks=%d, skip=%d, err=%d) [%ds]",
                         i + 1, total, status["embedded_ok"], status["chunks_added"],
                         status["skipped"], status["errors"], status["elapsed_s"])

        except Exception as e:
            status["errors"] += 1
            log.warning("embed_failed: %s: %s", filename, str(e)[:200])

    conn.close()

    status["active"] = False
    status["processed"] = total
    status["elapsed_s"] = round(time.time() - status["started_at"])
    status["stage"] = "done"
    write_status(status)

    log.info("Embedding complete: %d files, %d chunks embedded, %d skipped, %d errors in %ds",
             status["embedded_ok"], status["chunks_added"],
             status["skipped"], status["errors"], status["elapsed_s"])


if __name__ == "__main__":
    main()
