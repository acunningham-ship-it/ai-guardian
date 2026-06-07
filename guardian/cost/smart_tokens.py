"""Smart max_tokens — dynamically set output limits based on request type.

Output tokens cost 3-5x input tokens. Setting appropriate max_tokens
per task type is the single easiest cost reduction with zero quality loss.
"""
import re
from typing import Optional


# ── Task Type Classification ────────────────────────────────────────

class TaskType:
    SUMMARIZE = "summarize"        # Short summary of content
    EXPLAIN = "explain"            # Explanation/teaching
    LIST = "list"                  # Bullet list of items
    CODE_FIX = "code_fix"          # Small code fix
    CODE_WRITE = "code_write"      # Write new code
    CODE_REVIEW = "code_review"    # Code review output
    ANSWER = "answer"              # Direct factual answer
    CHAT = "chat"                  # General conversation
    ARCHITECT = "architect"        # Architecture/system design
    TRANSLATE = "translate"        # Translation
    REFACTOR = "refactor"          # Code refactor (larger scope)
    UNKNOWN = "unknown"


# Max output tokens per task type — calibrated to avoid unnecessary output
TASK_MAX_TOKENS = {
    TaskType.SUMMARIZE: 400,
    TaskType.EXPLAIN: 800,
    TaskType.LIST: 500,
    TaskType.CODE_FIX: 1000,
    TaskType.CODE_WRITE: 2000,
    TaskType.CODE_REVIEW: 1500,
    TaskType.ANSWER: 300,
    TaskType.CHAT: 1000,
    TaskType.ARCHITECT: 4000,
    TaskType.TRANSLATE: 2000,
    TaskType.REFACTOR: 2500,
    TaskType.UNKNOWN: 2000,
}

# Estimated savings vs default max_tokens=4096
TASK_SAVINGS_PCT = {
    TaskType.SUMMARIZE: 90,
    TaskType.EXPLAIN: 80,
    TaskType.LIST: 88,
    TaskType.CODE_FIX: 76,
    TaskType.CODE_WRITE: 51,
    TaskType.CODE_REVIEW: 63,
    TaskType.ANSWER: 93,
    TaskType.CHAT: 76,
    TaskType.ARCHITECT: 2,
    TaskType.TRANSLATE: 51,
    TaskType.REFACTOR: 39,
    TaskType.UNKNOWN: 51,
}


def classify_task(messages: list[dict]) -> str:
    """Classify the task type from the message content.
    
    Uses pattern matching on the last user message.
    Returns a TaskType string.
    """
    if not messages:
        return TaskType.UNKNOWN

    # Use the last user message for classification
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        return TaskType.UNKNOWN

    last_msg = user_msgs[-1].get("content", "").strip().lower()
    text = last_msg

    # Also check if there's code context in the conversation
    all_text = " ".join(m.get("content", "") for m in messages).lower()
    has_code = "```" in all_text or "def " in all_text or "class " in all_text

    # ── High-priority patterns (check first) ─────────────────────

    # Summarize
    if re.search(r'\b(summarize|summary|summarise|tldr|tl;dr|brief|condensed|overview)\b', text):
        return TaskType.SUMMARIZE

    # ── Code-related patterns (specific BEFORE generic) ───────────

    # Code fix (small fix to existing code)
    if re.search(r'\b(fix|bug|error|issue|broken|debug|patch|repair|correct)\b', text) and has_code:
        return TaskType.CODE_FIX

    # Code review
    if re.search(r'\b(review|audit|check|analyze|critique|feedback)\b', text) and has_code:
        return TaskType.CODE_REVIEW

    # Refactor
    if re.search(r'\b(refactor|refactoring|restructure|reorganize|clean up|rewrite)\b', text) and has_code:
        return TaskType.REFACTOR

    # Architecture / system design
    if re.search(r'\b(architect|design (a |the )?(system|service|api|database|schema|architecture|microservice|infrastructure|platform))\b', text):
        return TaskType.ARCHITECT

    # Translate
    if re.search(r'\b(translate|translation|in (spanish|french|german|japanese|chinese|korean|portuguese|italian))\b', text):
        return TaskType.TRANSLATE

    # Explain
    if re.search(r'\b(explain|how does|what does|why does|teach me|walk me through|describe)\b', text):
        return TaskType.EXPLAIN

    # Direct answer
    if re.search(r'^(what is|who is|when was|where is|how many|is it|can you|does it|true or false|yes or no)\b', text):
        return TaskType.ANSWER

    # List (needs to be specific enough to not match "write a function to sort a list")
    if re.search(r'\b(list all|list the|enumerate|name all|what are|give me \d+|top \d+)\b', text):
        return TaskType.LIST
    if re.search(r'^(list)\b', text):  # "List ..." at start of message
        return TaskType.LIST

    # Code write (new code) — checked AFTER specific patterns
    if re.search(r'\b(write|implement|create|build|develop|code|function|class|module|script|program)\b', text):
        return TaskType.CODE_WRITE

    if has_code and len(text) < 200:
        # Short prompt with code context = likely a fix/answer
        return TaskType.CODE_FIX

    # ── Fallback ──────────────────────────────────────────────────

    # Short messages = probably chat
    if len(text) < 100:
        return TaskType.CHAT

    return TaskType.UNKNOWN


def compute_smart_max_tokens(
    messages: list[dict],
    requested_max_tokens: Optional[int] = None,
    default_max_tokens: int = 4096,
    enforce: bool = True,
) -> dict:
    """Compute the optimal max_tokens for this request.
    
    Args:
        messages: The conversation messages
        requested_max_tokens: What the client requested (respect this if set)
        default_max_tokens: System default max
        enforce: If True, override client's max_tokens with our smart value.
                 If False, only reduce if client didn't specify.
    
    Returns:
        dict with max_tokens, task_type, savings_pct, original_max_tokens
    """
    task_type = classify_task(messages)
    smart_max = TASK_MAX_TOKENS.get(task_type, default_max_tokens)

    original = requested_max_tokens or default_max_tokens

    if enforce:
        # Always use smart value (but never exceed what client asked for)
        final_max = min(smart_max, original)
    else:
        # Only apply smart value if client didn't specify
        if requested_max_tokens is None:
            final_max = smart_max
        else:
            final_max = requested_max_tokens

    savings_pct = TASK_SAVINGS_PCT.get(task_type, 0)
    tokens_saved = max(0, original - final_max)

    return {
        "max_tokens": final_max,
        "task_type": task_type,
        "savings_pct": savings_pct,
        "original_max_tokens": original,
        "tokens_saved": tokens_saved,
        "changed": final_max != original,
    }
