"""
Document RAG tools — search, list, and delete uploaded documents.

docs.search  — find relevant chunks inside uploaded files
docs.list    — show all uploaded documents
docs.delete  — remove a document by ID
"""
from kai.tools.registry import registry
from kai.memory import documents as _docs
from kai._app_state import get_embed_fn as _get_embed_fn


@registry.tool(
    name="docs.search",
    description=(
        "Search through uploaded documents (PDFs, Word files, text files, code, CSV) "
        "to find relevant content. Use when the user asks about something that might be "
        "in a file they uploaded. Returns the most relevant passages with their source filename. "
        "If results are found, quote them directly and cite the document name. "
        "If nothing relevant is found, say so clearly."
    ),
    parameters={
        "query": {
            "type": "string",
            "description": "The search query — what to look for in the uploaded documents.",
            "required": True,
        },
        "top_k": {
            "type": "integer",
            "description": "Number of results to return (default 5, max 10).",
            "required": False,
        },
    },
)
def docs_search(query: str, top_k: int = 5) -> str:
    top_k = min(max(1, int(top_k)), 10)
    if not _docs.has_documents():
        return "No documents have been uploaded yet."
    results = _docs.search(query, embed_fn=_get_embed_fn(), top_k=top_k)
    if not results:
        return f"No relevant content found for: {query!r}"
    lines = [f"Found {len(results)} passage(s) matching {query!r}:\n"]
    for i, r in enumerate(results, 1):
        lines.append(
            f"[{i}] Source: {r['doc_name']}  (chunk {r['chunk_index'] + 1})\n"
            f"{r['content'].strip()}\n"
        )
    return "\n".join(lines)


@registry.tool(
    name="docs.list",
    description=(
        "List all documents that have been uploaded for retrieval. "
        "Shows filename, type, size, and upload date for each document. "
        "Use when the user asks what files are available or what documents Kai can search."
    ),
)
def docs_list() -> str:
    docs = _docs.list_documents()
    if not docs:
        return "No documents uploaded yet."
    lines = [f"{len(docs)} document(s) available:\n"]
    for d in docs:
        kb = round(d["char_count"] / 1000, 1)
        date = d["uploaded_at"][:10]
        lines.append(
            f"  • {d['filename']}  [{d['file_type']}]  ~{kb}k chars  "
            f"({d['chunk_count']} chunks)  uploaded {date}"
            f"  ID: {d['doc_id']}"
        )
    return "\n".join(lines)


@registry.tool(
    name="docs.delete",
    description=(
        "Delete an uploaded document by its document ID. "
        "Removes the document and all its searchable chunks from memory. "
        "Use docs.list first to get the document ID if needed."
    ),
    parameters={
        "doc_id": {
            "type": "string",
            "description": "The document ID to delete (from docs.list output).",
            "required": True,
        },
    },
)
def docs_delete(doc_id: str) -> str:
    ok = _docs.delete_document(doc_id.strip())
    if ok:
        return f"Document {doc_id!r} deleted successfully."
    return f"Document {doc_id!r} not found."
