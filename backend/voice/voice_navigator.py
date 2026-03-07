"""
voice_navigator.py — Cross-page navigation by voice (Stage 3.5)

Resolves voice navigation commands to executor actions using buttons/links
scanned by extractor.js. Zero LLM. <2ms.

Examples:
  "go to purchase history"  → finds link → doClick
  "open new purchase"       → finds link → doClick
  "add a supplier"          → finds "Supplier (नवीन सप्लायर्स)" → doClick
  "go back"                 → window.history.back()
  "show me all purchases"   → finds "All-purchase" link → doClick
  "go to dashboard"         → window.location = "/"
"""
from __future__ import annotations

import logging
import random
import re

logger = logging.getLogger(__name__)


# ── Intent detection ──────────────────────────────────────────────────────────

_NAV_PATTERNS = re.compile(
    r"""
    \b(?:go\s+to|open|navigate\s+to|take\s+me\s+to|show\s+me|
         switch\s+to|load|launch|chalo|jao|dikha|khol)            # go to X
    |
    \b(?:add|create|new|naveen|navi)\s+(?:a\s+|an\s+)?            # add a supplier
    |
    \b(?:go\s+back|back|previous\s+page|wapas)                    # go back
    |
    \b(?:go\s+home|home\s+page|dashboard|main\s+page|ghar)        # go home
    |
    \b(?:refresh|reload|dobara\s+khol)                            # refresh
    """,
    re.IGNORECASE | re.VERBOSE,
)

_BACK_RE    = re.compile(r"\b(go\s+back|back|previous\s+page|wapas\s+jao|wapas)\b", re.I)
_HOME_RE    = re.compile(r"\b(go\s+home|home\s+page|main\s+page|dashboard|ghar)\b", re.I)
_REFRESH_RE = re.compile(r"\b(refresh|reload|dobara\s+khol)\b", re.I)

_TARGET_RE = re.compile(
    r"""
    \b(?:go\s+to|open|navigate\s+to|take\s+me\s+to|
         show\s+me|switch\s+to|load|launch|chalo|khol|dikha)\s+(.+)
    |
    \b(?:add|create|new|naveen)\s+(?:a\s+|an\s+)?(.+?)
        (?:\s+(?:form|page|record|section))?$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Words to strip before matching nav targets
_NAV_NOISE = {
    "the", "a", "an", "me", "to", "for", "new", "form", "page",
    "screen", "section", "view", "tab", "module", "window",
    "please", "can", "you", "i", "want", "need",
}

# Synonym map — spoken words → what to look for in nav labels
_NAV_SYNONYMS: dict[str, list[str]] = {
    "purchase":  ["purchase", "khareedi", "buy", "procure", "po"],
    "sales":     ["sales", "sell", "vikri", "invoice"],
    "supplier":  ["supplier", "vendor", "saplayer", "party"],
    "customer":  ["customer", "client", "grahak"],
    "history":   ["history", "itihas", "all", "past", "log"],
    "stock":     ["stock", "inventory", "item", "product"],
    "ledger":    ["ledger", "bill", "account"],
    "dashboard": ["dashboard", "home", "main"],
    "report":    ["report", "analytics"],
}


def is_navigation_intent(instruction: str) -> bool:
    """Return True if this looks like a navigation command."""
    return bool(_NAV_PATTERNS.search(instruction))


def resolve_navigation(
    instruction: str,
    context:     dict,
    current_url: str = "",
) -> dict | None:
    """
    Resolve a navigation instruction to an executor action.

    Returns one of:
        {"action": "navigate", "url": "back",   "spoken": "..."}
        {"action": "navigate", "url": "reload", "spoken": "..."}
        {"action": "navigate", "url": "/",      "spoken": "..."}
        {"action": "click",    "field_id": ..., "label": ..., "href": ..., "spoken": "..."}
        None  →  could not resolve
    """
    # ── Special: back / home / refresh ───────────────────────────────────────
    if _BACK_RE.search(instruction):
        return {
            "action": "navigate",
            "url":    "back",
            "spoken": random.choice([
                "Going back to the previous page.",
                "Sure, taking you back.",
                "Going back.",
            ]),
        }

    if _HOME_RE.search(instruction):
        # Try to find a dashboard/home link in the nav first (most reliable)
        dedicated_nav = context.get("nav_links", [])
        buttons = context.get("buttons", [])
        nav_links = dedicated_nav if dedicated_nav else [b for b in buttons if b.get("is_nav") and not b.get("disabled")]
        if not nav_links:
            nav_links = [b for b in buttons if not b.get("disabled")]
        
        dash_tokens = _expand_tokens(["dashboard", "home"])
        best_btn, best_score = None, 0
        for btn in nav_links:
            s = _score_button(btn.get("label", ""), dash_tokens)
            if s > best_score:
                best_score, best_btn = s, btn
        
        if best_btn and best_score >= 4:
            label = best_btn.get("label", "Dashboard")
            clean = re.sub(r"\([^)]*\)", "", label).strip()
            clean = re.sub(r"[\u0900-\u0D7F]+", "", clean).strip() or label
            return {
                "action":   "click",
                "field_id": best_btn.get("button_id", ""),
                "label":    label,
                "href":     best_btn.get("href"),
                "spoken":   random.choice([
                    f"Taking you to {clean}.",
                    "Opening the main dashboard.",
                    "Heading to the dashboard.",
                ]),
            }
        
        # Fallback: navigate to hash root (SPA apps use #/ not /)
        return {
            "action": "navigate",
            "url":    "hash_home",   # executor handles this as location.hash = "/"
            "spoken": random.choice([
                "Taking you to the main dashboard.",
                "Heading back to the home page.",
                "Opening the dashboard.",
            ]),
        }

    if _REFRESH_RE.search(instruction):
        return {
            "action": "navigate",
            "url":    "reload",
            "spoken": "Refreshing the page.",
        }

    # ── Extract target ────────────────────────────────────────────────────────
    target = _extract_target(instruction)
    if not target:
        return None

    logger.info("Nav intent — target: '%s'", target)

    # ── Collect all nav links from context ────────────────────────────────────
    # nav_links[] = sidebar anchors (new extractor); buttons[] = form/action buttons
    # Fall back to buttons with is_nav=True for older extractor versions
    dedicated_nav = context.get("nav_links", [])
    buttons       = context.get("buttons", [])

    if dedicated_nav:
        nav_links    = [b for b in dedicated_nav if not b.get("disabled")]
        form_buttons = [b for b in buttons if not b.get("disabled")]
    elif buttons:
        nav_links    = [b for b in buttons if b.get("is_nav") and not b.get("disabled")]
        form_buttons = [b for b in buttons if not b.get("is_nav") and not b.get("disabled")]
        if not nav_links:
            nav_links    = [b for b in buttons if not b.get("disabled")]
            form_buttons = []
    else:
        return None

    # Strip extra noise from target: "panel", "section", "module" etc.
    _EXTRA_NOISE = {"panel", "section", "module", "area", "part", "region",
                    "navigation", "nav", "menu", "sidebar"}
    clean_target = " ".join(w for w in target.split() if w.lower() not in _EXTRA_NOISE)
    target_tokens = _expand_tokens(_tokenise(clean_target or target))

    def _best_in(pool: list[dict]) -> tuple[dict | None, int]:
        best, score = None, 0
        for btn in pool:
            s = _score_button(btn.get("label", ""), target_tokens)
            if s > score:
                score, best = s, btn
        return best, score

    best_btn, best_score = _best_in(nav_links)
    if best_score < 4:
        fb, fs = _best_in(form_buttons)
        if fs > best_score:
            best_btn, best_score = fb, fs

    MIN_SCORE = 4
    if best_btn is None or best_score < MIN_SCORE:
        logger.info("Nav: no match for '%s' (best=%d)", target, best_score)
        return None

    label  = best_btn.get("label", target)
    spoken = _build_nav_spoken(label, target)

    logger.info("Nav: '%s' → '%s' [score=%d]", target, label, best_score)

    return {
        "action":   "click",
        "field_id": best_btn.get("button_id", ""),
        "label":    label,
        "href":     best_btn.get("href"),
        "spoken":   spoken,
    }


def get_available_nav_labels(context: dict, max_n: int = 6) -> list[str]:
    """Return the top nav link labels visible on the page — for error messages."""
    buttons = context.get("buttons", [])
    nav = [b.get("label","") for b in buttons if b.get("is_nav") and not b.get("disabled")]
    # Clean bilingual labels
    cleaned = []
    for lbl in nav:
        clean = re.sub(r"\([^)]*\)", "", lbl).strip()
        clean = re.sub(r"[\u0900-\u0D7F]+", "", clean).strip()
        if clean and len(clean) > 1:
            cleaned.append(clean)
    # Deduplicate
    seen = set()
    out  = []
    for c in cleaned:
        if c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out[:max_n]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_target(instruction: str) -> str:
    m = _TARGET_RE.search(instruction)
    if not m:
        return ""
    target = (m.group(1) or m.group(2) or "").strip()
    return target.lower()


def _tokenise(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if w not in _NAV_NOISE and len(w) > 1]


def _expand_tokens(tokens: list[str]) -> list[str]:
    """Expand tokens with synonyms — 'purchase' also matches 'khareedi'."""
    expanded = list(tokens)
    for tok in tokens:
        for canonical, syns in _NAV_SYNONYMS.items():
            if tok == canonical or tok in syns:
                expanded.extend(syns)
    return list(dict.fromkeys(expanded))  # deduplicate, preserve order


def _score_button(label: str, target_tokens: list[str]) -> int:
    if not label or not target_tokens:
        return 0

    # Strip Marathi/Devanagari from label before scoring
    clean = re.sub(r"\([^)]*\)", "", label)
    clean = re.sub(r"[\u0900-\u0D7F]+", "", clean).strip()
    slug  = re.sub(r"[^a-z0-9]+", "_", clean.lower()).strip("_")
    label_tokens = _tokenise(slug)

    score = 0
    for tok in target_tokens:
        if tok in label_tokens:
            score += 10        # exact token match
        elif any(tok in lt or lt in tok for lt in label_tokens):
            score += 5         # substring match
        if tok in slug:
            score += 2         # slug contains token

    # Precise short labels score higher (avoid overly broad matches)
    if len(label_tokens) <= 3 and score > 0:
        score += 3

    return score


def _build_nav_spoken(label: str, original_target: str = "") -> str:
    """Build a rich, Copilot-like spoken confirmation for a nav action."""
    # Strip Marathi from label for spoken text
    clean = re.sub(r"\([^)]*\)", "", label).strip()
    clean = re.sub(r"[\u0900-\u0D7F]+", "", clean).strip()
    clean = clean or label

    # Add/Create pages
    if re.search(r"\b(add|create|new|naveen)\b", clean, re.I):
        return random.choice([
            f"Opening the {clean} form for you.",
            f"Sure, I'm loading the {clean} page.",
            f"Taking you to {clean}.",
        ])

    # History/list pages
    if re.search(r"\b(history|all|list|log)\b", clean, re.I):
        return random.choice([
            f"Opening {clean} — you'll see all your records there.",
            f"Loading {clean} for you.",
            f"Sure, taking you to {clean}.",
        ])

    # Generic
    return random.choice([
        f"Sure, opening {clean}.",
        f"Taking you to {clean} now.",
        f"Navigating to {clean}.",
    ])


# ── Error response ────────────────────────────────────────────────────────────

def navigation_spoken_error(instruction: str, context: dict | None = None) -> str:
    """Rich error message when no nav target found — lists available options."""
    target  = _extract_target(instruction) or instruction
    target  = target.strip().title()

    if context:
        available = get_available_nav_labels(context)
        if available:
            options = ", ".join(available[:4])
            more    = f" and {len(available)-4} more" if len(available) > 4 else ""
            return (
                f"I couldn't find a '{target}' section on this page. "
                f"I can see these navigation options: {options}{more}. "
                f"Say 'go to' followed by any of those."
            )

    return (
        f"I couldn't find '{target}' to navigate to. "
        "Try saying 'go to purchase', 'open supplier', or 'go back'."
    )