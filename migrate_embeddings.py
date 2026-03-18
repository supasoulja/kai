"""
migrate_embeddings.py
---------------------
Migrates the episodic_vec table from nomic-embed-text (768-dim)
to qwen3-embedding:4b (2560-dim).

Run once after pulling qwen3-embedding:4b:
    python migrate_embeddings.py

Handles both c:/newB (kai.db) and c:/newB-roof (roof.db) if they exist.
"""
import sqlite3
import sys
from pathlib import Path

import ollama
import sqlite_vec


NEW_DIM   = 2560
NEW_MODEL = "qwen3-embedding:4b"

DB_PATHS = [
    Path("kai/memory/kai's memory/kai.db"),
    Path("C:/newB-roof/kai/memory/roof memory/roof.db"),
]


def embed(text: str) -> list[float]:
    resp = ollama.embed(model=NEW_MODEL, input=text)
    return resp["embeddings"][0]


def migrate(db_path: Path) -> None:
    if not db_path.exists():
        print(f"  skip — not found: {db_path}")
        return

    print(f"\n[{db_path}]")
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    # Drop old vector table
    conn.execute("DROP TABLE IF EXISTS episodic_vec")
    conn.commit()
    print("  dropped old episodic_vec")

    # Recreate at new dimension
    conn.execute(f"CREATE VIRTUAL TABLE episodic_vec USING vec0(embedding float[{NEW_DIM}])")
    conn.commit()
    print(f"  created episodic_vec float[{NEW_DIM}]")

    # Fetch all text entries to re-embed
    rows = conn.execute(
        "SELECT rowid, id, content FROM episodic_entries"
    ).fetchall()

    if not rows:
        print("  no entries to embed — done")
        conn.close()
        return

    print(f"  embedding {len(rows)} entries with {NEW_MODEL}...")
    ok = 0
    for rowid, entry_id, content in rows:
        try:
            vec = embed(content)
            conn.execute(
                "INSERT INTO episodic_vec (rowid, embedding) VALUES (?, ?)",
                (rowid, sqlite_vec.serialize_float32(vec))
            )
            ok += 1
            if ok % 10 == 0:
                print(f"    {ok}/{len(rows)}")
        except Exception as e:
            print(f"    warning: failed to embed entry {entry_id}: {e}")

    conn.commit()
    conn.close()
    print(f"  done — {ok}/{len(rows)} entries re-embedded")


if __name__ == "__main__":
    print(f"Migration: nomic-embed-text (768-dim) → {NEW_MODEL} ({NEW_DIM}-dim)")

    # Verify model is available
    try:
        test = embed("test")
        if len(test) != NEW_DIM:
            print(f"ERROR: model returned {len(test)} dims, expected {NEW_DIM}")
            sys.exit(1)
        print(f"Model OK — verified {NEW_DIM}-dim output")
    except Exception as e:
        print(f"ERROR: could not reach {NEW_MODEL} via Ollama: {e}")
        print("Make sure 'ollama pull qwen3-embedding:4b' has finished.")
        sys.exit(1)

    for p in DB_PATHS:
        migrate(p)

    print("\nMigration complete. Restart web.py / cli.py.")
