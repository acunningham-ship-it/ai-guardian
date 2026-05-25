"""Quality guardrails — code validation, security scanning, performance checks."""
import ast
import re
from typing import Any, Optional

from guardian.models.schemas import QualityReport, QualityVerdict


# ── Security Patterns ───────────────────────────────────────────────

DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    # API keys / secrets in code
    (r'(?:api_key|apikey|api-key|secret|password|token)\s*=\s*["\'][^"\']{8,}["\']',
     "Hardcoded API key or secret detected"),
    (r'(?:sk-|pk-|ghp_|gho_)[a-zA-Z0-9]{20,}',
     "Potential API key in code"),
    # SQL injection risks
    (r'execute\s*\(\s*["\'].*%s',
     "Potential SQL injection (string formatting in query)"),
    (r'execute\s*\(\s*f["\']',
     "Potential SQL injection (f-string in query)"),
    (r'\.format\s*\).*(?:SELECT|INSERT|UPDATE|DELETE)',
     "Potential SQL injection (.format() in query)"),
    # Dangerous eval/exec
    (r'\beval\s*\(',
     "Dangerous eval() call detected"),
    (r'\bexec\s*\(',
     "Dangerous exec() call detected"),
    # Shell injection
    (r'os\.system\s*\(',
     "os.system() call — potential shell injection"),
    (r'subprocess\..*shell\s*=\s*True',
     "subprocess with shell=True — potential injection"),
    # Insecure deserialization
    (r'pickle\.loads?\s*\(',
     "Insecure pickle deserialization"),
    (r'yaml\.load\s*\([^)]*\)',
     "Unsafe yaml.load() — use yaml.safe_load()"),
    # Weak crypto
    (r'hashlib\.md5\s*\(',
     "Weak hash algorithm (MD5)"),
    (r'hashlib\.sha1\s*\(',
     "Weak hash algorithm (SHA1)"),
    # Debug mode left on
    (r'debug\s*=\s*True',
     "Debug mode enabled in production code"),
    # Hardcoded IPs / internal addresses
    (r'(?:192\.168\.|10\.|172\.(?:1[6-9]|2\d|3[01])\.)',
     "Hardcoded internal IP address"),
]

# ── Performance Anti-Patterns ──────────────────────────────────────

PERF_PATTERNS: list[tuple[str, str]] = [
    (r'for\s+.+\s+in\s+.+\s*:\s*\n\s*for\s+.+\s+in\s+.+\s*:\s*\n\s*for\s+',
     "Triple-nested loop — O(n^3) complexity risk"),
    (r'\.append\s*\(.*\).*\n.*\.append\s*\(.*\).*\n.*\.append\s*\(',
     "Repeated .append() in loop — consider list comprehension"),
    (r'for\s+.+\s+in\s+range\s*\(\s*len\s*\(',
     "Using range(len()) — consider enumerate() or direct iteration"),
    (r'str\s*\(\s*\)\s*\+\s*.*\+.*str\s*\(\s*\)',
     "String concatenation in loop — use join() or f-strings"),
    (r'(?:SELECT|select).*\n.*(?:SELECT|select).*\n.*(?:SELECT|select)',
     "Multiple queries in loop — N+1 query problem"),
    (r'time\.sleep\s*\(\s*\d+\s*\)',
     "Blocking sleep() — consider async/await"),
    (r'requests\.(?:get|post|put|delete)\s*\(.*\)\s*\n.*requests\.',
     "Sequential HTTP requests — consider async or batching"),
    (r'open\s*\([^)]*\)\s*\.\s*read\s*\(\s*\)\s*\n.*open\s*\(',
     "Multiple file opens — consider context manager or batching"),
]


def _extract_code_blocks(text: str) -> list[str]:
    """Extract code blocks from markdown or plain text."""
    # Markdown code blocks
    blocks = re.findall(r'```(?:\w+)?\n(.*?)```', text, re.DOTALL)
    if blocks:
        return blocks
    # If the whole thing looks like code
    if any(kw in text for kw in ["def ", "class ", "import ", "function", "const ", "var "]):
        return [text]
    return []


def _check_syntax(code: str) -> Optional[str]:
    """Try to parse Python code. Returns error message or None."""
    try:
        ast.parse(code)
        return None
    except SyntaxError as e:
        return f"Syntax error at line {e.lineno}: {e.msg}"
    except Exception:
        return None  # Not Python, skip


def _check_security(code: str) -> list[str]:
    """Scan code for security anti-patterns."""
    issues = []
    for pattern, description in DANGEROUS_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE | re.MULTILINE):
            issues.append(description)
    return issues


def _check_performance(code: str) -> list[str]:
    """Scan code for performance anti-patterns."""
    flags = []
    for pattern, description in PERF_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE | re.MULTILINE | re.DOTALL):
            flags.append(description)
    return flags


def _estimate_complexity(code: str) -> dict[str, Any]:
    """Rough complexity estimation."""
    lines = code.strip().split("\n")
    non_empty = [l for l in lines if l.strip() and not l.strip().startswith("#")]

    # Count control structures
    loops = len(re.findall(r'\b(for|while)\b', code))
    conditionals = len(re.findall(r'\b(if|elif|match)\b', code))
    functions = len(re.findall(r'\bdef\s+', code))
    classes = len(re.findall(r'\bclass\s+', code))

    return {
        "lines": len(non_empty),
        "loops": loops,
        "conditionals": conditionals,
        "functions": functions,
        "classes": classes,
    }


def _detect_hallucination_risk(messages: list[dict], response: str) -> Optional[str]:
    """
    Heuristic to detect potential hallucination in AI responses.
    Returns a warning string or None.
    """
    # Check for common hallucination markers
    hallucination_markers = [
        r"I cannot (?:find|access|verify)",
        r"As of my last (?:update|knowledge)",
        r"I(?:'m| am) not (?:sure|certain|able to)",
        r"This (?:may|might|could) (?:not be|be incorrect)",
        r"I(?:'m| am) (?:not )?aware of",
    ]

    for marker in hallucination_markers:
        if re.search(marker, response, re.IGNORECASE):
            return "Response contains uncertainty markers — verify claims independently"

    # Check if response contradicts the question
    user_msg = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
    if user_msg:
        # If user asks for a specific thing and response is very generic
        if len(user_msg) > 100 and len(response) < 50:
            return "Response seems too brief for the question — may be missing key details"

    return None


# ── Main Quality Check ─────────────────────────────────────────────

def check_quality(
    response: str,
    messages: list[dict],
    enable_security: bool = True,
    enable_performance: bool = True,
) -> QualityReport:
    """
    Run quality guardrails on an AI response.
    Returns a QualityReport with verdict, score, and issues.
    """
    issues: list[dict[str, Any]] = []
    suggestions: list[str] = []
    security_issues: list[str] = []
    performance_flags: list[str] = []
    score = 100.0

    code_blocks = _extract_code_blocks(response)

    if not code_blocks:
        # Non-code response — just check for hallucination
        hallucination = _detect_hallucination_risk(messages, response)
        if hallucination:
            score -= 15
            issues.append({"type": "hallucination_risk", "detail": hallucination})
        return QualityReport(
            verdict=QualityVerdict.PASS if score >= 70 else QualityVerdict.WARN,
            score=max(0, score),
            issues=issues,
            suggestions=suggestions,
            hallucination_risk=hallucination,
        )

    # Analyze each code block
    for i, code in enumerate(code_blocks):
        block_label = f"code block {i+1}"

        # Syntax check
        syntax_error = _check_syntax(code)
        if syntax_error:
            score -= 30
            issues.append({"type": "syntax_error", "block": block_label, "detail": syntax_error})
            suggestions.append(f"Fix syntax errors in {block_label}")

        # Security scan
        if enable_security:
            sec_issues = _check_security(code)
            if sec_issues:
                score -= len(sec_issues) * 10
                security_issues.extend(sec_issues)
                for issue in sec_issues:
                    issues.append({"type": "security", "block": block_label, "detail": issue})
                suggestions.append(f"Address security issues in {block_label}")

        # Performance check
        if enable_performance:
            perf_issues = _check_performance(code)
            if perf_issues:
                score -= len(perf_issues) * 5
                performance_flags.extend(perf_issues)
                for issue in perf_issues:
                    issues.append({"type": "performance", "block": block_label, "detail": issue})
                suggestions.append(f"Review performance in {block_label}")

        # Complexity analysis
        complexity = _estimate_complexity(code)
        if complexity["lines"] > 200:
            score -= 5
            issues.append({
                "type": "complexity",
                "block": block_label,
                "detail": f"Large code block ({complexity['lines']} lines) — consider breaking into smaller functions",
            })
            suggestions.append(f"Break {block_label} into smaller, testable functions")

        if complexity["loops"] >= 3:
            score -= 10
            issues.append({
                "type": "complexity",
                "block": block_label,
                "detail": f"Nested loops detected ({complexity['loops']}) — potential performance issue",
            })

    # Hallucination check on the overall response
    hallucination = _detect_hallucination_risk(messages, response)
    if hallucination:
        score -= 15
        issues.append({"type": "hallucination_risk", "detail": hallucination})

    # Determine verdict
    score = max(0, min(100, score))
    if score >= 80:
        verdict = QualityVerdict.PASS
    elif score >= 50:
        verdict = QualityVerdict.WARN
    else:
        verdict = QualityVerdict.FAIL

    return QualityReport(
        verdict=verdict,
        score=round(score, 1),
        issues=issues,
        suggestions=suggestions,
        security_issues=security_issues,
        performance_flags=performance_flags,
        hallucination_risk=hallucination,
    )
