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


def assess_content(markdown: str, title: str = "", status_code: int = 200) -> dict:
    status_code = status_code or 200
    text = markdown.strip()
    heading = title.strip().lower()
    sample = text[:1500].lower()
    gate = (
        "verify you are human",
        "checking your browser",
        "just a moment",
        "access denied",
        "attention required",
        "enable javascript and cookies",
        "complete the captcha",
        "security verification",
        "подтвердите, что вы человек",
    )
    reasons = []
    if status_code in {401, 403, 429}:
        reasons.append("http_gate_" + str(status_code))
    # A matching gate title is strong evidence; short page text must be directive.
    if any(phrase in heading for phrase in gate) or (
        len(text) < 1800
        and any(sample.startswith(phrase) or sample.startswith("# " + phrase) for phrase in gate)
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
