"""Content assessment distinguishes gate pages from discussion about gates."""

import re
import ipaddress
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def canonical_url(url: str) -> str:
    p = urlsplit(url.strip())
    if p.scheme.lower() not in {"http", "https"} or not p.hostname or p.username:
        raise ValueError("Only public HTTP(S) URLs without credentials are supported")
    if os.environ.get("LOCAL_WEB_RESEARCH_ALLOW_PRIVATE", "").lower() not in {"1", "true"}:
        host_name = p.hostname.lower()
        if (
            host_name == "localhost"
            or host_name.endswith((".localhost", ".local", ".internal"))
            or "." not in host_name
        ):
            raise ValueError("Private targets require explicit configuration")
        try:
            address = ipaddress.ip_address(host_name)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError("Private targets require explicit configuration")
    port = p.port
    host = p.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    if port and not (
        p.scheme.lower() == "https" and port == 443 or p.scheme.lower() == "http" and port == 80
    ):
        host += ":" + str(port)
    tracking = {"fbclid", "gclid", "msclkid", "yclid", "_ga", "mc_cid", "mc_eid"}
    query = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in tracking
    ]
    return urlunsplit((p.scheme.lower(), host, p.path or "/", urlencode(query), ""))


_CHALLENGE_DIRECTIVES = (
    "verify you are human",
    "checking your browser",
    "just a moment",
    "attention required",
    "enable javascript and cookies",
    "complete the captcha",
    "security verification",
    "подтвердите, что вы человек",
    "please confirm that you are not a robot",
    "confirm that you are not a robot",
    "are you not a robot",
    "подтвердите, что вы не робот",
    "вы не робот",
)


def _is_gate_directive(value: str, directives: tuple[str, ...]) -> bool:
    normalized = " ".join(value.lower().split()).strip(" .!?…:#")
    return normalized in directives


def _primary_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        line = line.strip()
        if not line or re.fullmatch(r"!?\[[^\]]*\]\([^\n]+\)", line):
            continue
        heading = re.fullmatch(r"#{1,6}\s+(.+?)(?:\s+#+)?", line)
        return heading.group(1) if heading else line
    return ""


def is_captcha_gate(markdown: str, title: str = "") -> bool:
    """Match challenge directives, never CAPTCHA mentions inside an article."""
    if any(
        _is_gate_directive(value, _CHALLENGE_DIRECTIVES)
        for value in (title, _primary_heading(markdown))
    ):
        return True
    # Product branding alone also appears on ordinary documentation pages.
    return _is_gate_directive(title, ("smartcaptcha", "yandex smartcaptcha")) and any(
        _is_gate_directive(line, ("i'm not a robot", "я не робот", "press to continue"))
        for line in markdown.splitlines()
    )


def assess_content(
    markdown: str, title: str = "", status_code: int = 200, captcha: dict | None = None
) -> dict:
    if captcha and (
        captcha.get("status") in {"unexpected_page", "challenge_changed"}
        or captcha.get("challenge_observed")
        and captcha.get("status") != "solved"
    ):
        return {"status": "blocked", "reasons": ["captcha_target_unverified"]}
    status_code = status_code or 200
    text = markdown.strip()
    reasons = []
    if status_code in {401, 403, 429}:
        reasons.append("http_gate_" + str(status_code))
    if is_captcha_gate(text, title) or any(
        _is_gate_directive(value, ("access denied",)) for value in (title, _primary_heading(text))
    ):
        reasons.append("challenge_or_access_gate")
    if reasons:
        return {"status": "blocked", "reasons": reasons}
    if status_code >= 400:
        return {"status": "failed", "reasons": ["http_" + str(status_code)]}
    if not text:
        return {"status": "failed", "reasons": ["empty_content"]}
    if len(re.findall(r"\w+", text)) < 40:
        return {"status": "partial", "reasons": ["insufficient_content"]}
    return {"status": "read", "reasons": []}
