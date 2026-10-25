"""Secret redaction applied to every tool output, trace line and report field."""

import re

# Well-known credential formats: always redacted.
_TOKEN_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL),
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),                 # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),              # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b"),    # OpenAI / Anthropic style keys
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),           # Slack
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),                   # Google API key
    re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{16,}\b"),         # Stripe
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
]

# key = "value" assignments whose key name looks secret-ish. Quoted literals of 6+ chars,
# or unquoted (.env / YAML style) values that look like tokens (contain a digit, no code syntax).
_KEY = r"[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|credential)[\w.-]*"
_ASSIGNMENT = re.compile(
    rf"""(?ix)
    (?P<key>{_KEY})
    (?P<sep>["']?\s*[:=]\s*)
    (?: (?P<q>["'])(?P<qval>[^\s"']{{6,}})(?P=q)
      | (?P<val>(?=[A-Za-z0-9_+/=-]*\d)[A-Za-z0-9_+/=-]{{8,}})(?![\w(.\[]) )
    """
)
# URLs with embedded credentials: scheme://user:pass@host
_URL_CREDS = re.compile(r"(?P<pre>[a-z][a-z0-9+.-]*://[^\s:/@]+:)(?P<val>[^\s@/]{3,})(?P<post>@)", re.IGNORECASE)

# Values that are clearly not real secrets: env lookups, templates, placeholders.
_PLACEHOLDER = re.compile(
    r"(?i)^(\$\{?|<|%|\{\{|os\.|process\.env|env\(|getenv|none|null|true|false|changeme|change_me|"
    r"your|example|placeholder|dummy|xxx|\*\*\*|redacted|secrets\.|https?://)"
)


def _mask(value: str) -> str:
    return f"[REDACTED len={len(value)}]"


def redact(text: str) -> str:
    if not text:
        return text
    for pat in _TOKEN_PATTERNS:
        text = pat.sub(lambda m: _mask(m.group(0)), text)
    text = _URL_CREDS.sub(lambda m: m.group("pre") + _mask(m.group("val")) + m.group("post"), text)

    def _assign(m: re.Match) -> str:
        val = m.group("qval") or m.group("val")
        q = m.group("q") or ""
        if _PLACEHOLDER.match(val) or val.startswith("[REDACTED"):
            return m.group(0)
        return f"{m.group('key')}{m.group('sep')}{q}{_mask(val)}{q}"

    return _ASSIGNMENT.sub(_assign, text)
