"""Aggregate externally judged search cases without deriving labels from ranker scores.

Input is a list of cases with results containing URL and label (0 off-topic,
1 topical, 2 usable primary, null unknown). Labels depend on the query purpose
and require independent review. An official domain alone is not label 2.
Missing judgments stay unknown; precision bounds use five slots per query,
including empty slots. Primary discovery does not establish source admission.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit


def valid_host(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        _ = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"https", "http"} or not host or parsed.username or parsed.password:
        return None
    if any(character.isspace() for character in url):
        return None
    host = host.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
        labels = ascii_host.split(".")
        if len(ascii_host) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
        ):
            return None
    return host


def domain_matches(host: str, domain: str) -> bool:
    domain = domain.lower().strip().rstrip(".")
    if not domain or any(character in domain for character in "/:@ "):
        raise ValueError("Allowed domains must be bare hostnames")
    return host == domain or host.endswith("." + domain)


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Return numeric metrics and uncertainty, never private query/source text."""
    relevant = unknown_top5 = primary_hits = unknown_primary = 0
    returned_top5 = judged_top5 = invalid = escaped = domain_total = 0
    entity_escaped = entity_unknown = entity_total = 0
    empty = degraded = fallback = 0
    latencies = []
    for case in cases:
        results = case.get("results", [])
        empty += not bool(results)
        degraded += bool(case.get("degraded", False))
        fallback += bool(case.get("fallback_used", False))
        latency = case.get("latency_seconds")
        if latency is not None:
            if isinstance(latency, bool) or not math.isfinite(latency) or latency < 0:
                raise ValueError("Latency must be a finite nonnegative number")
            latencies.append(latency)
        labels = []
        domains = case.get("allowed_domains", [])
        for result in results:
            label = result.get("label")
            if label is not None and (type(label) is not int or label not in {0, 1, 2}):
                raise ValueError("Labels must be 0, 1, 2 or null")
            labels.append(label)
            host = valid_host(result.get("url", ""))
            invalid += host is None
            if domains:
                domain_total += 1
                escaped += host is None or not any(domain_matches(host, d) for d in domains)
            if case.get("requires_entities", False):
                judgment = result.get("entity_compliant")
                if judgment is not None and type(judgment) is not bool:
                    raise ValueError("Entity compliance must be a boolean or null")
                entity_total += 1
                entity_escaped += judgment is False
                entity_unknown += judgment is None
        first = labels[:5]
        returned_top5 += len(first)
        judged_top5 += sum(label is not None for label in first)
        relevant += sum(label in {1, 2} for label in first)
        unknown_top5 += sum(label is None for label in first)
        top10 = labels[:10]
        if 2 in top10:
            primary_hits += 1
        elif None in top10:
            unknown_primary += 1
    count = len(cases)
    latencies.sort()
    p95 = latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else None
    return {
        "cases": count,
        "precision_at_5_lower": _rate(relevant, count * 5),
        "precision_at_5_upper": _rate(relevant + unknown_top5, count * 5),
        "top5_judgment_coverage": _rate(judged_top5, returned_top5),
        "primary_hit_at_10_lower": _rate(primary_hits, count),
        "primary_hit_at_10_upper": _rate(primary_hits + unknown_primary, count),
        "primary_hit_unknown_cases": unknown_primary,
        "invalid_results": invalid,
        "domain_escaped_results": escaped,
        "domain_compliance": _rate(domain_total - escaped, domain_total),
        "domain_checked_results": domain_total,
        "entity_escaped_results": entity_escaped,
        "entity_unknown_results": entity_unknown,
        "entity_checked_results": entity_total,
        "entity_compliance_lower": _rate(
            entity_total - entity_escaped - entity_unknown, entity_total
        ),
        "entity_compliance_upper": _rate(entity_total - entity_escaped, entity_total),
        "latency_observed_cases": len(latencies),
        "latency_mean_seconds": sum(latencies) / len(latencies) if latencies else None,
        "latency_p95_seconds": p95,
        "empty_rate": _rate(empty, count),
        "degraded_rate": _rate(degraded, count),
        "fallback_rate": _rate(fallback, count),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(json.loads(args.cases.read_text())), indent=2))


if __name__ == "__main__":
    main()
