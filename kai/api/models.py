"""
Pydantic request models for the web API.

Extracted from web.py so the request/response shapes live in one small module
that routers (and web.py itself) can import without pulling in the whole app.
"""
from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str

class LoginRequest(BaseModel):
    name: str
    pin:  str

class FeedbackRequest(BaseModel):
    message_id: int
    value: int        # 1 = thumbs up, -1 = thumbs down
    snippet: str = "" # first ~300 chars of the response (for episodic memory)

class FactUpdateRequest(BaseModel):
    value: str

class ModeRequest(BaseModel):
    mode: str  # "short" | "long" | "chat" | "research"

class AddModelRequest(BaseModel):
    name: str
    ollama_id: str
    think: bool = False

class PresetRequest(BaseModel):
    preset: str   # "thinking" | "normal" | "creative" | "crazy"

class TemperatureRequest(BaseModel):
    temperature: float   # per-thread override, clamped to config bounds

class PresetTempsRequest(BaseModel):
    temps: dict[str, float]   # preset key -> custom temperature

class ToolLevelRequest(BaseModel):
    level: str   # "light" | "balanced" | "deep" | "off" — which model runs tool rounds

class ToolToggleRequest(BaseModel):
    name: str      # tool name, e.g. "system.kill_process"
    enabled: bool  # True = on, False = turned off for this user

class RecipeRequest(BaseModel):
    name: str                       # lowercase, filename-safe
    description: str = ""
    triggers: list[str] = []        # keyword hints
    steps: list[str] = []           # ordered tool calls, e.g. "system.info"

class GreetingRequest(BaseModel):
    fresh: bool = False   # True = new-chat clean-start greeting (no welcome-back note)

class WatchdogRegisterRequest(BaseModel):
    join_code: str
    label: str = ""

class WatchdogEventRequest(BaseModel):
    device_id: str
    device_key: str
    script_id: str
    severity: str
    message: str
    suggestion: str = ""

class NodeResultRequest(BaseModel):
    result: dict
    error: bool = False


# ── Study mode ──────────────────────────────────────────────────────────────

class StudySearchRequest(BaseModel):
    query: str
    filter: str = "all"   # "all" | "papers" | "books"

class StudyFindFreeRequest(BaseModel):
    doi: str = ""
    title: str = ""

class StudyDownloadRequest(BaseModel):
    url: str
    title: str = ""
    author: str = ""
    source: str = ""
    format: str = "epub"   # "epub" | "pdf"

class StudyAskRequest(BaseModel):
    question: str
