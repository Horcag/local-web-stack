"""Independent judgment aggregation, uncertainty and acceptance safety metrics."""

import pytest

from scripts.quality.search_relevance import summarize


def hit(label, url="https://reference.example/document", **kwargs):
    return {"url": url, "label": label, **kwargs}


def test_empty_results_lower_precision_and_primary_discovery():
    metrics = summarize([{"results": [hit(2)]}, {"results": []}])
    assert metrics["precision_at_5_lower"] == 0.1
    assert metrics["precision_at_5_upper"] == 0.1
    assert metrics["primary_hit_at_10_lower"] == 0.5
    assert metrics["empty_rate"] == 0.5


def test_unknown_labels_remain_bounds_and_ignore_ranker_scores():
    metrics = summarize([{"results": [hit(0, score=100), hit(None, score=100)]}])
    assert metrics["precision_at_5_lower"] == 0
    assert metrics["precision_at_5_upper"] == 0.2
    assert metrics["top5_judgment_coverage"] == 0.5
    assert metrics["primary_hit_at_10_lower"] == 0
    assert metrics["primary_hit_at_10_upper"] == 1


def test_primary_hit_is_limited_to_ten_results():
    metrics = summarize([{"results": [hit(1)] * 10 + [hit(2)]}])
    assert metrics["precision_at_5_lower"] == 1
    assert metrics["primary_hit_at_10_upper"] == 0


def test_official_domain_alone_does_not_prove_primary_usefulness():
    metrics = summarize([{"results": [hit(None)], "allowed_domains": ["reference.example"]}])
    assert metrics["domain_compliance"] == 1
    assert metrics["primary_hit_at_10_lower"] == 0
    assert metrics["primary_hit_unknown_cases"] == 1


def test_domains_accept_subdomains_and_reject_suffix_spoofing():
    metrics = summarize(
        [
            {
                "allowed_domains": ["reference.example"],
                "results": [
                    hit(1, "https://sub.reference.example/a"),
                    hit(0, "https://reference.example.attacker.example/a"),
                    hit(0, "https://otherreference.example/a"),
                ],
            },
            {"results": [hit(1, "https://unconstrained.example/a")]},
        ]
    )
    assert metrics["domain_checked_results"] == 3
    assert metrics["domain_escaped_results"] == 2
    assert metrics["domain_compliance"] == pytest.approx(1 / 3)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "/relative",
        "https://",
        "https://user:pass@host.example",
        "https://host.example:invalid",
        "https://host.example/a b",
        "https://.",
        "https://-bad.example",
        "https://bad..example",
    ],
)
def test_invalid_urls_are_counted_and_fail_domain_compliance(url):
    metrics = summarize([{"allowed_domains": ["host.example"], "results": [hit(0, url)]}])
    assert metrics["invalid_results"] == 1
    assert metrics["domain_escaped_results"] == 1


def test_entity_unknown_judgments_do_not_pass_acceptance():
    metrics = summarize(
        [
            {
                "requires_entities": True,
                "results": [
                    hit(1, entity_compliant=True),
                    hit(0, entity_compliant=False),
                    hit(None),
                ],
            }
        ]
    )
    assert metrics["entity_escaped_results"] == 1
    assert metrics["entity_unknown_results"] == 1
    assert metrics["entity_checked_results"] == 3
    assert metrics["entity_compliance_lower"] == pytest.approx(1 / 3)
    assert metrics["entity_compliance_upper"] == pytest.approx(2 / 3)


def test_latency_and_operational_rates():
    metrics = summarize(
        [
            {"latency_seconds": 1, "degraded": True},
            {"latency_seconds": 3, "fallback_used": True, "results": [hit(1)]},
            {"results": [hit(1)]},
        ]
    )
    assert metrics["latency_observed_cases"] == 2
    assert metrics["latency_mean_seconds"] == 2
    assert metrics["latency_p95_seconds"] == 3
    assert metrics["degraded_rate"] == pytest.approx(1 / 3)
    assert metrics["fallback_rate"] == pytest.approx(1 / 3)


def test_no_cases_has_unknown_rates_not_perfect_scores():
    metrics = summarize([])
    assert metrics["cases"] == 0
    assert metrics["precision_at_5_lower"] is None
    assert metrics["primary_hit_at_10_lower"] is None
    assert metrics["domain_compliance"] is None
    assert metrics["entity_checked_results"] == 0
    assert metrics["latency_p95_seconds"] is None


@pytest.mark.parametrize("label", [-1, 3, True, "2", 1.5])
def test_invalid_labels_fail_instead_of_becoming_relevant(label):
    with pytest.raises(ValueError, match="Labels"):
        summarize([{"results": [hit(label)]}])


@pytest.mark.parametrize("latency", [-1, float("nan"), float("inf"), True])
def test_invalid_latency_fails(latency):
    with pytest.raises(ValueError, match="Latency"):
        summarize([{"latency_seconds": latency}])
