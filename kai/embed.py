"""
Fast CPU-based embedding for live operations.

Uses ONNX Runtime + tokenizers to produce 384-dim vectors without touching
the GPU.  This eliminates VRAM contention between the chat model and the
embedding model — the single biggest latency source on 8 GB cards.

Model: Xenova/bge-small-en-v1.5 (33M params, 384-dim, ONNX quantized ~25 MB)
Downloaded from HuggingFace Hub on first run, cached in ~/.cache/kai/

At shutdown, `shutdown_reembed()` runs the heavy Ollama qwen3-embedding:4b
model to write high-quality 2560-dim vectors into shadow tables
(episodic_vec_hq, rag_chunks_vec_hq) for future use.

Public API
----------
    embed(text)         -> list[float]          # single 384-dim vector
    embed_batch(texts)  -> list[list[float]]    # batch of 384-dim vectors
    warm_up()                                   # pre-load model (call at startup)
    shutdown_reembed()                          # HQ re-embed at shutdown
"""
import os
import threading
from pathlib import Path

import numpy as np

import kai.config as cfg

# ── Singleton state ──────────────────────────────────────────────────────────

_session = None     # onnxruntime.InferenceSession
_tokenizer = None   # tokenizers.Tokenizer
_lock = threading.Lock()

# Model files are downloaded once to this cache directory
_CACHE_DIR = Path.home() / ".cache" / "kai" / "embed_model"


def _download_model() -> Path:
    """
    Download the ONNX model + tokenizer from HuggingFace Hub.
    Returns the local directory containing model.onnx and tokenizer.json.
    """
    model_dir = _CACHE_DIR / cfg.FAST_EMBED_MODEL.replace("/", "--")
    model_path = model_dir / "onnx" / "model_quantized.onnx"
    tokenizer_path = model_dir / "tokenizer.json"

    if model_path.exists() and tokenizer_path.exists():
        return model_dir

    model_dir.mkdir(parents=True, exist_ok=True)
    print(f"[~] Downloading embedding model {cfg.FAST_EMBED_MODEL}...")

    from huggingface_hub import hf_hub_download

    # The Xenova repo puts ONNX files in onnx/ subfolder, tokenizer at root
    hf_hub_download(
        repo_id=cfg.FAST_EMBED_MODEL,
        filename="onnx/model_quantized.onnx",
        local_dir=str(model_dir),
    )
    hf_hub_download(
        repo_id=cfg.FAST_EMBED_MODEL,
        filename="tokenizer.json",
        local_dir=str(model_dir),
    )

    print(f"[+] Model cached at {model_dir}")
    return model_dir


def _ensure_model():
    """Lazy-load the ONNX model + tokenizer. Thread-safe, called at most once."""
    global _session, _tokenizer
    if _session is not None:
        return

    with _lock:
        if _session is not None:
            return

        model_dir = _download_model()

        import onnxruntime as ort
        from tokenizers import Tokenizer

        # CPU-only, no GPU contention
        opts = ort.SessionOptions()
        physical_cores = max((os.cpu_count() or 4) // 2, 1)
        opts.inter_op_num_threads = 2
        opts.intra_op_num_threads = physical_cores
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        _session = ort.InferenceSession(
            str(model_dir / "onnx" / "model_quantized.onnx"),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        _tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        # bge-small-en-v1.5 max length is 512 tokens
        _tokenizer.enable_truncation(max_length=512)
        _tokenizer.enable_padding(pad_id=0, pad_token="[PAD]", length=None)


def _embed_texts(texts: list[str]) -> np.ndarray:
    """
    Core embedding: tokenize + ONNX inference + mean pooling + L2 normalize.
    Returns shape (len(texts), 384).
    """
    _ensure_model()

    # Tokenize
    encodings = _tokenizer.encode_batch(texts)
    input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
    token_type_ids = np.zeros_like(input_ids, dtype=np.int64)

    # ONNX inference
    feeds = {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "token_type_ids": token_type_ids,
    }
    outputs = _session.run(None, feeds)
    # outputs[0] is the last hidden state: (batch, seq_len, hidden_dim)
    last_hidden = outputs[0]

    # Mean pooling (mask-aware)
    mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
    sum_embeddings = np.sum(last_hidden * mask_expanded, axis=1)
    sum_mask = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
    embeddings = sum_embeddings / sum_mask

    # L2 normalize
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.clip(norms, a_min=1e-12, a_max=None)
    embeddings = embeddings / norms

    return embeddings


# ── Public API ───────────────────────────────────────────────────────────────

def embed(text: str) -> list[float]:
    """Embed a single string. Returns a 384-dim float list (~5 ms on CPU)."""
    result = _embed_texts([text])
    return result[0].tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a list of strings in one call. Returns list of 384-dim float lists."""
    if not texts:
        return []
    # Process in batches of 64 to limit memory usage
    all_vecs = []
    for i in range(0, len(texts), 64):
        batch = texts[i : i + 64]
        vecs = _embed_texts(batch)
        all_vecs.extend(v.tolist() for v in vecs)
    return all_vecs


def warm_up() -> None:
    """
    Pre-load the ONNX model at startup.
    First call downloads ~70 MB from HuggingFace (cached after that).
    Subsequent calls are instant.
    """
    _ensure_model()
    # Quick sanity check
    test = _embed_texts(["test"])
    dim = test.shape[1]
    print(f"[+] Fast embed ready  ({cfg.FAST_EMBED_MODEL}, {dim}-dim, CPU)")


# ── Shutdown: HQ re-embed ────────────────────────────────────────────────────

def shutdown_reembed() -> None:
    """
    Re-embed all stored entries with the heavy Qwen model into shadow HQ tables.

    Called at server/CLI shutdown when the chat model is no longer loaded,
    so Ollama can use the full GPU for embedding without contention.

    Best-effort — failures are printed but never raise.
    """
    from kai.db import get_conn, sqlite_vec_available

    if not sqlite_vec_available():
        return

    # Check if Ollama is alive
    from kai.brain import OllamaClient
    ollama = OllamaClient(cfg.OLLAMA_BASE_URL)
    if not ollama.is_alive():
        print("[!] Ollama not running — skipping HQ re-embed")
        return

    import sqlite_vec

    conn = get_conn()

    # ── Ensure HQ shadow tables exist ────────────────────────────────────
    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS episodic_vec_hq
        USING vec0(embedding float[{cfg.HQ_EMBED_DIM}])
    """)
    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_vec_hq
        USING vec0(embedding float[{cfg.HQ_EMBED_DIM}])
    """)
    conn.commit()

    # ── Episodic entries ─────────────────────────────────────────────────
    rows = conn.execute(
        "SELECT rowid, content FROM episodic_entries WHERE entry_type != 'turn'"
    ).fetchall()

    if rows:
        existing = set()
        try:
            for (rid,) in conn.execute("SELECT rowid FROM episodic_vec_hq").fetchall():
                existing.add(rid)
        except Exception:
            pass

        to_embed = [(rid, content) for rid, content in rows if rid not in existing]

        if to_embed:
            print(f"[~] HQ re-embed: {len(to_embed)} episodic entries...")
            texts = [content for _, content in to_embed]
            try:
                embeddings = ollama.embed_batch(texts, model=cfg.HQ_EMBED_MODEL)
                for (rid, _), emb in zip(to_embed, embeddings):
                    conn.execute(
                        "INSERT OR REPLACE INTO episodic_vec_hq (rowid, embedding) VALUES (?, ?)",
                        (rid, sqlite_vec.serialize_float32(emb)),
                    )
                conn.commit()
                print(f"[+] HQ re-embed: {len(to_embed)} episodic entries done")
            except Exception as exc:
                print(f"[!] HQ episodic re-embed failed: {exc}")

    # ── RAG chunks ───────────────────────────────────────────────────────
    rag_rows = conn.execute(
        "SELECT rowid, content FROM rag_chunks"
    ).fetchall()

    if rag_rows:
        existing_rag = set()
        try:
            for (rid,) in conn.execute("SELECT rowid FROM rag_chunks_vec_hq").fetchall():
                existing_rag.add(rid)
        except Exception:
            pass

        to_embed_rag = [(rid, content) for rid, content in rag_rows if rid not in existing_rag]

        if to_embed_rag:
            print(f"[~] HQ re-embed: {len(to_embed_rag)} RAG chunks...")
            texts = [content for _, content in to_embed_rag]
            try:
                batch_size = 64
                for i in range(0, len(texts), batch_size):
                    batch_texts = texts[i : i + batch_size]
                    batch_items = to_embed_rag[i : i + batch_size]
                    embeddings = ollama.embed_batch(batch_texts, model=cfg.HQ_EMBED_MODEL)
                    for (rid, _), emb in zip(batch_items, embeddings):
                        conn.execute(
                            "INSERT OR REPLACE INTO rag_chunks_vec_hq (rowid, embedding) VALUES (?, ?)",
                            (rid, sqlite_vec.serialize_float32(emb)),
                        )
                    conn.commit()
                print(f"[+] HQ re-embed: {len(to_embed_rag)} RAG chunks done")
            except Exception as exc:
                print(f"[!] HQ RAG re-embed failed: {exc}")
