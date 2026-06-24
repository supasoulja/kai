"""
Tool gate — decides, per turn, whether the model should be offered tools and
whether the turn warrants chain-of-thought reasoning.

This is a keyword/phrase/regex subsystem that used to live at the top of
brain.py. It was lifted out so brain.py stays focused on the conversation loop
and the gate has its own home (and test surface). brain.py imports the public
entry points: _query_needs_tools(), _query_needs_thinking(), and the compiled
_TOOL_SIGNALS regex (also used during tool-schema selection).
"""
import re

# ── Tool signal detection ─────────────────────────────────────────────────────
# Categorized keyword lists composed into one regex at module load.
# Adding a new tool category = add a list entry here.
# Long term these lists are meant to shrink: the handoff router can open the
# tool gate semantically (see SEMANTIC_TOOL_GATE in config.py), which doesn't
# need a keyword per tool.

_TOOL_KEYWORDS_SINGLE = [
    # System / hardware
    "time", "date", "clock", "weather", "cpu", "gpu", "ram", "memory", "disk",
    "drive", "ssd", "hdd", "hardware", "storage", "monitor", "upgrade",
    "startup", "boot", "autostart", "autorun", "space",
    # Processes / performance
    "process", "processes", "running", "usage", "load", "speed", "fan", "volt",
    "spec", "specs", "performance", "slow", "fast", "laggy", "lagging",
    # Network
    "network", "wifi", "ethernet", "dns", "gateway", "ping", "tracert",
    "traceroute", "latency", "jitter", "bandwidth", "connectivity",
    # PC / system
    "pc", "computer", "machine", "check", "system",
    # Errors / crashes
    "crash", "crashes", "log", "logs", "error", "errors",
    # Notes / search
    "note", "notes", "remind", "search", "find", "web", "internet",
    # Memory tree
    "remember", "forget", "remembered",
    # Goals
    "goal", "goals",
    # Skills
    "skill", "skills", "health check", "cleanup",
    # Steam / games
    "steam", "benchmark", "benchmarks", "fps",
    # Hardware parts
    "motherboard", "mobo", "psu", "nvme", "sata",
    "ryzen", "threadripper", "epyc", "xeon", "geforce", "radeon", "arc",
    # Documents
    "document", "pdf",
]

_TOOL_KEYWORDS_COMPOUND = [
    # Temperature / temps (word-boundary safe)
    r"temp(?:erature)?",
    # Lag variants
    r"lag(?:s|ging|gy)?",
    # Updates / patches
    r"update(?:s)?", r"patch(?:es)?",
    # Frame rate
    r"frame\s*rate",
    # Event / Windows logs
    r"event\s*log", r"windows\s*log", r"system\s*error",
    # IP / Wi-Fi
    r"ip\s*address", r"wi-fi", r"internet\s*connection",
    # Windows update
    r"windows\s*update",
    # File size triggers
    r"large\s*file", r"big\s*file", r"folder\s*size",
    r"old\s*file", r"recent\s*file",
    r"free\s*up", r"clean\s*up", r"cleanup",
    # Connection
    r"connection\s*test", r"slow\s*internet", r"high\s*ping", r"packet\s*loss",
    # Hardware upgrade
    r"should\s+i\s+(?:buy|get|upgrade)", r"worth\s+(?:it|buying|getting)",
    r"performance\s+(?:gain|delta|improvement)",
    r"compatible|compatibility|socket|am[45]|lga\d+|ddr[45]|pcie",
    r"cpu\s+cooler|aio\s+cooler|power\s+supply",
    r"m\.2|gen\s*[345]|pcie\s*[45]",
    r"versus|comparison|better\s+than|faster\s+than",
    # DLL / error codes
    r"\w+\.dll",
    # Document triggers
    r"docx?|word\s+doc|uploaded?\s+file|my\s+file|that\s+file",
    # Gaming triggers
    r"gaming\s+time|game\s+time|game\s+mode|ready\s+to\s+play|pre.?game",
]

_TOOL_PHRASE_PATTERNS = [
    # Question patterns
    r"what.{0,20}(?:running|using|taking|my)",
    r"how.{0,20}(?:perform|fast|slow|much)",
    r"can you.{0,20}(?:check|see|look|find|get|test)",
    r"my (?:pc|cpu|gpu|ram|disk|specs|system|network|ip|files?|drive|internet|connection|ping)",
    # Action + target patterns
    r"(?:free|clear|clean).{0,15}(?:space|storage|disk)",
    r"what.{0,20}(?:eating|using).{0,10}(?:space|disk|storage)",
    r"(?:test|check|diagnose).{0,20}(?:network|internet|connection|ping|lag)",
    r"i.{0,10}(?:lag|lagging|ping|connection).{0,20}(?:game|games|server|high|bad)",
    r"(?:speed\s*up|fix|optimize|clean\s*up|scan|tune|boost).{0,25}(?:pc|computer|system|windows|disk)",
    r"(?:my\s+(?:pc|computer|system)).{0,30}(?:slow|lag|problem|issue|wrong|broken|fix)",
    r"(?:what.{0,10}wrong|health\s*check|diagnose).{0,20}(?:pc|computer|system)",
    r"(?:restore\s*point|undo\s*changes|rollback|revert\s*changes)",
    r"make.{0,15}(?:pc|computer|system).{0,15}(?:faster|better|run)",
    # File read/write triggers
    r"(?:read|open|show|view|cat|print).{0,20}(?:file|code|script|config|log|\.py|\.txt|\.json|\.md|\.log|\.yaml|\.toml)",
    r"(?:what.{0,15}in|contents?\s+of|look\s+at|show\s+me).{0,20}(?:file|folder|directory|\.py|\.txt|\.json|\.md)",
    r"(?:list|browse|explore).{0,15}(?:files?|folder|directory|dir\b)",
    r"\.(?:py|txt|json|md|log|yaml|yml|toml|ini|cfg|js|ts|html|css|sh|bat|ps1)\b",
    # Search / current-info triggers
    r"(?:latest|newest|most\s+recent|current)\s+(?:game|news|update|patch|version|release|trailer|price)",
    # Workspace / file-write triggers
    r"(?:write|create|save|make).{0,20}(?:file|script|code|txt|log|config|\.py|\.txt|\.json|\.md)",
    r"(?:append|add\s+to|add\s+a\s+line).{0,20}(?:file|notes?|log)",
    r"(?:edit|change|update|fix).{0,20}(?:file|script|code|config)\b",
    r"(?:clone|pull|download).{0,20}(?:repo|repository|git|code\s*base)",
    r"git\s+(?:clone|pull|push|status|log|commit)",
    # Game/trending triggers
    r"what.{0,20}(?:game|games).{0,20}(?:popular|trending|hot|new|out|good|recommend|play)",
    r"what.{0,15}(?:new|out|trending|popular|hot)\s+(?:right\s+now|this\s+(?:week|month|year)|in\s+202[5-9])",
    r"(?:right\s+now|these\s+days|in\s+202[5-9]|currently).{0,20}(?:popular|trending|good|out|best|top)",
    r"(?:price|cost|how\s+much).{0,20}(?:is|does|for|costs?)",
    r"news\s+about|latest\s+news|what\s+happened\s+to",
    r"(?:is|has).{0,10}(?:been\s+)?(?:released|out|announced|launched|available).{0,15}(?:yet|now)?",
    # Error code / DLL fault triggers
    r"0x[0-9a-fA-F]{4,}",
    r"(?:fix|solve|debug).{0,25}(?:crash|error|fault|exception|freeze|hang)",
    r"(?:crash|error|fault|exception).{0,25}(?:fix|solve|debug|help|what)",
    # Steam / game library triggers
    r"my\s+(?:installed\s+)?(?:games?|game\s+library|steam\s+library)",
    r"installed\s+games?|games?\s+installed",
    r"what\s+games?\s+(?:do\s+i\s+have|i\s+have|i\s+own|are\s+on)",
    # Hardware comparison triggers
    r"(?:should\s+i|is\s+it\s+worth|do\s+i\s+need).{0,30}(?:new|upgrade|replace|buy|get)",
    r"(?:will\s+it\s+(?:fit|work|be\s+compatible)|does\s+it\s+(?:support|work\s+with))",
    # Document / RAG triggers
    r"(?:search|find|look).{0,20}(?:in|through|inside|across).{0,20}(?:document|file|pdf|upload)",
    r"(?:what.{0,15}in|contents?\s+of|summarize|explain).{0,20}(?:document|pdf|file|upload)",
    r"(?:list|show).{0,15}(?:uploaded?|my)\s+(?:file|document|pdf)",
    r"(?:delete|remove).{0,15}(?:document|file|pdf)",
    # Memory tree triggers
    r"what.{0,20}(?:know|remember|recall).{0,15}about",
    r"(?:know|remember)\s+(?:about\s+)?me\b",
    # Past-session recall by recency — "what were we doing last?", "before your
    # last reset", "pick up where we left off". These carry no searchable keyword,
    # so without an explicit gate signal the tool is never offered (the bug that
    # made Kai claim she had no record of the previous session).
    r"(?:what|where).{0,20}(?:we|you|i)\b.{0,20}(?:doing|working|talking|discussing|left\s+off|up\s+to|last)",
    r"(?:last|previous|earlier|prior)\s+(?:session|time|conversation|chat|thing)",
    r"(?:before|since|after)\s+(?:your|the|my|our|last)?\s*(?:last\s+)?(?:reset|restart|reboot|sleep|shutdown|session|crash)",
    r"pick\s+up\s+where|where\s+(?:we|you|i)\s+left\s+off|catch\s+me\s+up|what\s+did\s+we",
    # Self-inspection triggers
    r"(?:your|the)\s+(?:source|code|brain|internals|implementation|tools?|memory\s+system)",
    r"how\s+(?:do|does|are)\s+you\s+(?:work|think|run|function|operate|decide)",
    r"(?:show|read|look\s+at|inspect).{0,15}(?:your|own).{0,15}(?:code|source|brain|file)",
    r"(?:update|check|change|edit).{0,15}(?:persona|identity|your\s+(?:rules|behavior))",
    r"(?:new|missing|undocumented).{0,15}(?:feature|tool|capability)",
    # Sandbox / file management triggers
    r"(?:move|copy|rename|delete|trash|organize|clean\s*up).{0,20}(?:file|folder|directory|dir\b)",
    r"(?:move|copy|rename|delete|trash).{0,5}(?:it|that|this|them)",
    r"(?:put|bring|copy).{0,15}(?:into|to|in).{0,15}(?:workspace|kaifil)",
    r"approve|go\s+ahead|do\s+it|execute\s+(?:it|that|the)",
    # Conversational system-inspection triggers (natural phrasing, no hardware noun)
    r"(?:check|look\s+at|scan|inspect|examine|monitor|sense).{0,15}(?:your\s+|the\s+)?surroundings?",
    r"(?:notice|see|spot|sense|find).{0,20}(?:anything|something).{0,15}(?:different|unusual|weird|odd|off|wrong|new|strange|amiss)",
    r"(?:anything|something).{0,15}(?:different|unusual|weird|odd|off|wrong|new|strange|amiss)",
    r"(?:dig|look|go|search|scan|check|probe|investigate).{0,10}(?:deeper|closer|harder|further|more)",
    r"\b(?:deeper|closer)\b.{0,15}(?:scan|look|check|dig)",
    r"(?:double|triple)[-\s]*check|take\s+another\s+look|look\s+(?:again|around)|check\s+again",
    r"(?:run|do|perform|start).{0,15}(?:a\s+|another\s+)?(?:scan|sweep|check|diagnostic|health\s*check|inspection)",
    r"\b(scan|sweep|diagnose|inspect)\b",
    r"(?:what'?s|how'?s).{0,15}(?:going\s+on|happening|the\s+status|everything\s+look)",
]

# Compose single keywords into \b(word1|word2|...)\b, then join with phrase patterns.
_single_pattern = r"\b(" + "|".join(re.escape(w) for w in _TOOL_KEYWORDS_SINGLE) + r")\b"
_compound_pattern = r"\b(" + "|".join(_TOOL_KEYWORDS_COMPOUND) + r")\b"
_phrase_pattern = "|".join(_TOOL_PHRASE_PATTERNS)
_TOOL_SIGNALS = re.compile(
    f"{_single_pattern}|{_compound_pattern}|{_phrase_pattern}",
    re.IGNORECASE,
)

# Continuation idioms that leave a bare tool keyword behind once the follow-up
# words are stripped — "one more time" / "run that again". The residual "time"
# would otherwise read as a direct time.now request, so strip these too before
# testing whether a follow-up turn carries its own intent.
_CONTINUATION_NOUNS = re.compile(
    r"\b(one\s*more\s*time|another\s*time|once\s*more|time|again)\b",
    re.IGNORECASE,
)

# Short confirmations that delegate a task — "go ahead", "yes do it", "proceed", etc.
_FOLLOW_UP_SIGNALS = re.compile(
    r"\b(go\s*ahead|proceed|do\s*(it|that|what|them)|yes(\s*please)?|sure(\s*thing)?|"
    r"ok(ay)?|sounds?\s*good|continue|carry\s*on|keep\s*going|"
    r"you\s*can(\s*do)?|please\s*do|do\s*what\s*you\s*(need|want|think)|"
    # continuations that ask Kai to go further on the previous task
    r"deeper|closer|harder|further|again|more|once\s*more|"
    r"anything\s*else|what\s*else|keep\s*looking|look\s*more)\b",
    re.IGNORECASE,
)


# ── Auto-think: skip reasoning for trivial prompts ────────────────────────────
# When reasoning mode is ON, this classifier decides per-prompt whether to
# actually send think=True to Ollama. Simple greetings, single-word replies,
# and casual chat skip thinking entirely — saving 10-30s of wasted compute.
# Complex queries (multi-step, debugging, comparisons, analysis) keep it on.

# Patterns that ALWAYS skip thinking (cheap chat)
_TRIVIAL_PATTERNS = re.compile(
    r"^("
    # Greetings
    r"h(ello|i|ey|owdy|iya|eya?)(\s+(there|kai|buddy|dude|bro|man|friend))?"
    r"|yo\b"
    r"|sup\b"
    r"|what'?s?\s*up"
    r"|good\s+(morning|afternoon|evening|night)"
    r"|g'?(morning|night)"
    r"|gm\b|gn\b"
    # Farewells
    r"|bye\b|goodbye|see\s*ya|later|night|cya|peace|ttyl"
    # Acknowledgements
    r"|ok(ay)?|k\b|sure|yep|yup|yeah|yes|no|nah|nope|mhm|hmm"
    r"|thanks?(\s*(you|a?\s*lot|so\s+much|kai))?"
    r"|ty\b|thx\b"
    r"|got\s*it|understood|makes?\s*sense|fair\s*(enough)?"
    r"|nice|cool|neat|sick|dope|bet|based|lol|lmao|haha|rofl"
    r"|wow|whoa|damn|dang|huh|oh|ah|oof|rip"
    # Simple identity / small talk
    r"|how\s+are\s+you(\s+doing)?(\s+today)?"
    r"|how'?s?\s*it\s+going"
    r"|what\s+are\s+you(\s+up\s+to)?"
    r"|who\s+are\s+you"
    r"|what'?s?\s+your\s+name"
    r"|tell\s+me\s+(about\s+)?yourself"
    r"|you\s+there\??"
    r"|are\s+you\s+(awake|alive|there|ready|up)"
    r")[\s?!.,]*$",
    re.IGNORECASE,
)

# Patterns that ALWAYS need thinking (complex reasoning)
_COMPLEX_PATTERNS = re.compile(
    r"("
    r"explain.{0,30}(how|why|difference|between|vs|versus)"
    r"|compare|contrast|analyze|evaluat"
    r"|step.by.step|walk\s+me\s+through"
    r"|pros?\s+and\s+cons?"
    r"|debug|diagnos|troubleshoot"
    r"|why\s+(is|does|did|would|should|can'?t|won'?t|isn'?t|doesn'?t|aren'?t)"
    r"|how\s+(would|should|could|do)\s+(?:i|you|we).{10,}"
    r"|what.{0,10}(best|optimal|right|correct)\s+(way|approach|method|strategy)"
    r"|design|architect|implement|refactor|optimize"
    r"|trade.?off|down.?side|caveat|implication"
    r"|write\s+(?:a\s+)?(?:function|class|script|program|code|algorithm)"
    r"|fix\s+(?:this|the|my)\s+(?:code|bug|error|issue|problem)"
    r")",
    re.IGNORECASE,
)


def _query_needs_thinking(query: str) -> bool:
    """Decide whether a query warrants chain-of-thought reasoning.

    Returns False for trivial prompts (greetings, acks, small talk).
    Returns True for complex prompts (analysis, debugging, comparisons).
    For ambiguous prompts, uses word count as a heuristic — longer = more likely complex.
    """
    stripped = query.strip()
    if not stripped:
        return False
    # Fast path: trivial patterns never need thinking
    if _TRIVIAL_PATTERNS.match(stripped):
        return False
    # Fast path: complex patterns always need thinking
    if _COMPLEX_PATTERNS.search(stripped):
        return True
    # Heuristic: very short prompts (< 8 words) are usually casual
    word_count = len(stripped.split())
    if word_count < 8:
        return False
    return True


def _query_needs_tools(query: str, history: list[dict] | None = None) -> str | None:
    """How the tool gate opened: "direct", "follow_up", or None (no tools).

    Both non-None values are truthy, so boolean use still works; the caller
    needs the distinction because follow-up turns must not drive tool
    SELECTION with their own (contentless) embedding.
    """
    # Does the query carry its OWN tool intent, independent of any follow-up
    # words? "ok maybe try the weather" names weather — a new direct request,
    # not a continuation of the prior turn. We test the residual left after
    # stripping follow-up words and continuation idioms ("one more time"); if a
    # tool signal survives, the turn is direct and must NOT borrow stale intent.
    follow = _FOLLOW_UP_SIGNALS.search(query)
    residual = _CONTINUATION_NOUNS.sub(" ", _FOLLOW_UP_SIGNALS.sub(" ", query))
    residual_direct = bool(_TOOL_SIGNALS.search(residual))

    # Continuation first, for SHORT messages: "run that one more time" matches
    # the bare keyword "time" and would otherwise classify as direct — but its
    # meaning lives in the previous turns, so selection must borrow intent
    # from there. Short + follow-up phrasing + recent tool context = follow_up
    # — but only when the message has no standalone intent of its own.
    if (history and len(query.split()) <= 8
            and follow and not residual_direct):
        for msg in history[-4:]:
            if _TOOL_SIGNALS.search(msg.get("content", "")):
                return "follow_up"
    # Direct signal: the message itself asks for something tool-worthy.
    if _TOOL_SIGNALS.search(query):
        return "direct"
    # Continuation: longer follow-ups like "ok then dig deeper into all of it"
    # still inherit intent from the recent conversation. If any of the last
    # few turns (either role) were about the system or a tool, keep tools
    # available so Kai actually re-checks instead of fabricating a scan.
    if history and follow and not residual_direct:
        for msg in history[-4:]:
            if _TOOL_SIGNALS.search(msg.get("content", "")):
                return "follow_up"
    return None
