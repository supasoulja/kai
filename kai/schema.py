from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar
from pydantic import BaseModel


# ── Memory types ───────────────────────────────────────────────────────────────

@dataclass
class SemanticFact:
    key: str
    value: str
    source: str = "conversation"      # how it was learned
    confidence: float = 1.0
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class ProceduralRule:
    key: str
    value: str
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class EpisodicEntry:
    id: str                           # UUID
    content: str                      # the text that was embedded
    timestamp: datetime
    entry_type: str                   # "turn", "summary", "event"
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None


@dataclass
class RelationshipEntry:
    id: str
    timestamp: datetime
    entry_type: str                   # "milestone", "tone_shift", "founding"
    content: str


# ── Context block ──────────────────────────────────────────────────────────────

@dataclass
class ContextBlock:
    identity: str
    procedural: list[ProceduralRule]
    semantic: list[SemanticFact]
    episodic: list[EpisodicEntry]
    # Volatile session stats — in-memory only, never persisted
    session_state: dict[str, str] = field(default_factory=dict)
    # Active D&D campaign block — injected when DM mode is on
    campaign: str = ""
    # Relevant document chunks from uploaded files (RAG)
    rag_chunks: list[dict] = field(default_factory=list)
    # Brief inventory of all uploaded docs — always injected so Kai knows they exist
    doc_inventory: list[dict] = field(default_factory=list)
    # Memory directory — tiny always-injected summary of what data exists
    memory_directory: str = ""

    # Class-level constant: human-readable labels for session state keys
    _SESSION_LABELS: ClassVar[dict[str, str]] = {
        "cpu_pct":       "CPU",
        "ram_pct":       "RAM",
        "disk_pct":      "Disk",
        "gpu_temp_c":    "GPU temp",
        "cpu_temp_c":    "CPU temp",
        "startup_count": "Startup programs",
        "disk_free_gb":  "Disk free",
    }
    _SESSION_UNITS: ClassVar[dict[str, str]] = {
        "cpu_pct":    "%",
        "ram_pct":    "% used",
        "disk_pct":   "% used",
        "gpu_temp_c": "°C",
        "cpu_temp_c": "°C",
        "disk_free_gb": " GB free",
    }

    def render(self) -> str:
        parts = []

        if self.identity:
            parts.append(f"[IDENTITY]\n{self.identity.strip()}")

        if self.memory_directory:
            parts.append(self.memory_directory)

        if self.procedural:
            lines = "\n".join(f"{r.key}={r.value}" for r in self.procedural)
            parts.append(f"[PROCEDURAL]\n{lines}")

        if self.semantic:
            lines = "\n".join(f"- {f.key}: {f.value}" for f in self.semantic)
            parts.append(f"[SEMANTIC — Your long-term memory. Every entry here is a fact you know.]\n{lines}")

        if self.episodic:
            lines = "\n".join(
                f"{e.timestamp.strftime('%Y-%m-%d %H:%M')}: {e.content}"
                for e in self.episodic
            )
            parts.append(f"[EPISODIC]\n{lines}")

        if self.session_state:
            pairs = []
            for k, v in self.session_state.items():
                label = self._SESSION_LABELS.get(k, k)
                unit  = self._SESSION_UNITS.get(k, "")
                pairs.append(f"{label}: {v}{unit}")
            parts.append(f"[SESSION]\n{', '.join(pairs)}")

        if self.campaign:
            parts.append(f"[CAMPAIGN]\n{self.campaign.strip()}")

        if self.rag_chunks:
            sections = []
            for chunk in self.rag_chunks:
                sections.append(f"[{chunk['doc_name']}]\n{chunk['content']}")
            parts.append("[DOCUMENTS]\n" + "\n\n---\n\n".join(sections))

        # Always show doc inventory so Kai knows what files the user has uploaded,
        # even when no chunks matched the current query.
        if self.doc_inventory and not self.rag_chunks:
            lines = []
            for doc in self.doc_inventory:
                lines.append(f"- {doc['filename']} ({doc['file_type']}, {doc['chunk_count']} chunks)")
            parts.append(
                "[UPLOADED FILES — The user has given you these documents. "
                "Use docs.search to read their content.]\n" + "\n".join(lines)
            )

        return "\n\n".join(parts)


# ── Brain I/O ──────────────────────────────────────────────────────────────────

class BrainResponse(BaseModel):
    type: str           # "final" | "tool"
    text: str = ""      # set when type == "final"
    tool_name: str = "" # set when type == "tool"
    tool_args: dict[str, Any] = {}


@dataclass
class ToolCall:
    trace_id: str
    tool_name: str
    args: dict[str, Any]


@dataclass
class ToolResult:
    trace_id: str
    tool_name: str
    success: bool
    output: Any
    error: str = ""
