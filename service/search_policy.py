"""Deterministic search constraints, per-result assessment and safe refinement."""

import ipaddress
import math
import re
import unicodedata
from urllib.parse import unquote, urlsplit

from .research.content import canonical_url
from .search_models import SearchRequest

_WORD = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
_SITE = re.compile(r"(?<![\w:-])site:([\w.-]+)", re.I)
_SITE_DOT = re.compile(r"(?<![\w:-])site\.([\w-]+(?:\.[\w-]+)+)", re.I)
_TOKEN = re.compile(r'"[^"\n]*"|\S+')
_STOP = set(
    "a an the of for and or in on to with by from about how what is are information please find search".split()
)
_OPTIONAL = {"please", "find", "search", "information", "about"}
_RESERVED = {"example.com", "example.net", "example.org"}


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _host(value: str) -> str:
    value = value.strip().lower().rstrip(".")
    if not value or any(c in value for c in "/ :?@#"):
        raise ValueError("Domains must be public hostnames")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Invalid hostname") from exc
    _public_host(value)
    return value


def _public_host(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if (
            len(labels) < 2
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in labels)
            or len(host) > 253
            or host.endswith(
                (
                    ".test",
                    ".invalid",
                    ".example",
                    ".localhost",
                    ".local",
                    ".internal",
                    ".onion",
                    ".arpa",
                )
            )
            or any(host == x or host.endswith("." + x) for x in _RESERVED)
            or labels[-1].isdigit()
        ):
            raise ValueError("Search result must have a public hostname")
    else:
        if not address.is_global:
            raise ValueError("Search result must have a public address")


def _search_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Search result must be HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Search result must not contain credentials")
    _host(parsed.hostname) if ":" not in parsed.hostname else _public_host(parsed.hostname)
    return canonical_url(value)


def _within(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _domain_constraints(structured: list[str], operators: list[str]) -> list[str]:
    if not structured or not operators:
        return structured or operators
    intersection = list(
        dict.fromkeys(
            child
            for left in structured
            for right in operators
            for child in (
                [left] if _within(left, right) else [right] if _within(right, left) else []
            )
        )
    )
    if not intersection:
        raise ValueError("Structured domains and site operators have disjoint scopes")
    return intersection


def prepare(request: SearchRequest) -> dict:
    query = " ".join(request.query.split())
    normalizations = []
    normalized_query = " ".join(
        token if token.startswith('"') else _SITE_DOT.sub(lambda m: "site:" + m[1], token)
        for token in _TOKEN.findall(query)
    )
    if normalized_query != query:
        normalizations.append(
            {"kind": "site_operator", "original": query, "normalized": normalized_query}
        )
    query = normalized_query
    structured = list(dict.fromkeys(_host(x) for x in request.domains))
    operators = []
    operator_text = " ".join(token for token in _TOKEN.findall(query) if not token.startswith('"'))
    for match in _SITE.finditer(operator_text):
        host = _host(match[1])
        if host not in operators:
            operators.append(host)
    domains = _domain_constraints(structured, operators)
    entities = list(dict.fromkeys(_normalized(x) for x in request.required_entities))
    quoted = [
        _normalized(x[1:-1]) for x in _TOKEN.findall(query) if x.startswith('"') and x.endswith('"')
    ]
    positive = []
    anchors = []
    for token in _TOKEN.findall(query):
        if token.startswith(("-", '"')) or ":" in token or token.upper() in {"OR", "AND", "NOT"}:
            continue
        for word in _WORD.findall(token):
            normalized = _normalized(word)
            if normalized not in _STOP:
                positive.append(normalized)
                if not word.isdigit() and (
                    any(c.isdigit() for c in word)
                    or word.isupper()
                    and len(word) > 1
                    or word[0].isupper()
                    and len(word) > 2
                ):
                    anchors.append(normalized)
    language = request.language
    if language == "auto":
        language = "ru" if re.search(r"[а-яё]", query, re.I) else "all"
    return {
        "query": query,
        "language": language,
        "domains": domains,
        "required_entities": entities,
        "normalizations": normalizations,
        "terms": list(dict.fromkeys(positive)),
        "quoted_terms": quoted,
        "anchors": list(dict.fromkeys(anchors)),
    }


def _contains(text: str, term: str) -> bool:
    # NFKC + casefold matching rejects entity substrings such as x4020 for x402.
    return re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text) is not None


def _engines(item: dict) -> list[str]:
    values = item.get("engines", [])
    if isinstance(values, str):
        values = [values]
    return list(dict.fromkeys(str(x) for x in [*values, item.get("engine", "")] if x))


def rank_results(items: list[dict], policy: dict) -> tuple[list[dict], list[dict]]:
    accepted: dict[str, dict] = {}
    rejected = []
    for item in items:
        reason = ""
        raw_url = str(item.get("url", ""))
        try:
            url = _search_url(raw_url)
        except (ValueError, UnicodeError):
            reason = "invalid_public_url"
            url = raw_url
        text = _normalized(
            " ".join(str(item.get(x, "")) for x in ("title", "snippet", "content"))
            + " "
            + unquote(url)
        )
        domains = policy.get("domains", [])
        entities = policy.get("required_entities", [])
        terms = policy.get("terms", [])
        # A generic hostname must not supply missing article topic evidence.
        topic_text = (
            _normalized(
                " ".join(str(item.get(x, "")) for x in ("title", "snippet", "content"))
                + " "
                + unquote(urlsplit(url).path + " " + urlsplit(url).query)
            )
            if not reason
            else ""
        )
        matched = [term for term in terms if _contains(topic_text, term)]
        host = (urlsplit(url).hostname or "") if not reason else ""
        if host and ":" not in host:
            host = _host(host)
        if not reason and domains and not any(_within(host, domain) for domain in domains):
            reason = "domain_mismatch"
        if not reason and any(not _contains(text, entity) for entity in entities):
            reason = "required_entity_missing"
        if not reason and any(not _contains(text, term) for term in policy.get("quoted_terms", [])):
            reason = "quoted_term_missing"
        anchors = policy.get("anchors", [])
        matched_anchors = [term for term in anchors if _contains(text, term)]
        if not reason and anchors and not matched_anchors:
            reason = "query_identifier_missing"
        minimum = min(len(terms), max(2, math.ceil(len(terms) / 2)))
        if not reason and len(matched) < minimum:
            reason = "insufficient_topic_match"
        if reason:
            rejected.append({"url": raw_url, "engine": item.get("engine", ""), "reason": reason})
            continue
        reasons = ["topic_match"] if matched else []
        if domains:
            reasons.append("domain_match")
        if entities:
            reasons.append("required_entities_match")
        if matched_anchors:
            reasons.append("query_entity_match")
        score = (
            10 * len(matched) / max(1, len(terms))
            + 3 * len(matched_anchors)
            + 10 * len(entities)
            + 10 * bool(domains)
        )
        try:
            provider_score = float(item.get("score", 0) or 0)
            if math.isfinite(provider_score):
                score += max(0, min(1, provider_score)) * 0.1
        except (TypeError, ValueError):
            pass
        result = dict(
            item, url=url, quality_score=round(score, 3), reasons=reasons, engines=_engines(item)
        )
        if url in accepted:
            previous = accepted[url]
            engines = list(dict.fromkeys(previous["engines"] + result["engines"]))
            if result["quality_score"] > previous["quality_score"]:
                accepted[url] = result
            accepted[url]["engines"] = engines
        else:
            accepted[url] = result
    return sorted(accepted.values(), key=lambda x: -x["quality_score"]), rejected


def refined_query(policy: dict) -> str | None:
    """Only remove optional boilerplate; preserve operators and all identity terms."""
    original = policy["query"]
    tokens = _TOKEN.findall(original)
    entities = policy.get("required_entities", [])
    refined = " ".join(
        token
        for token in tokens
        if token not in _OPTIONAL or any(_contains(entity, token) for entity in entities)
    )
    if (
        refined != original
        and refined
        and any(
            _WORD.search(token)
            for token in _TOKEN.findall(refined)
            if ":" not in token and not token.startswith("-")
        )
    ):
        return refined
    return None
