import pytest
from pydantic import ValidationError

from service.search_models import SearchRequest
from service.search_policy import prepare, rank_results, refined_query


def policy(query, **kwargs):
    return prepare(SearchRequest(query=query, **kwargs))


def result(url="https://records.fr/article", title="BODACC insolvency evidence", **kwargs):
    return {"url": url, "title": title, "engine": "duckduckgo", **kwargs}


@pytest.mark.parametrize(
    "language,expected", [("auto", "ru"), ("en", "en"), ("all", "all"), ("ru-RU", "ru-RU")]
)
def test_language(language, expected):
    assert policy("банкротство компании", language=language)["language"] == expected
    assert policy("company insolvency")["language"] == "all"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": " "},
        {"query": "x", "page": 101},
        {"query": "x", "max_results": 21},
        {"query": "x", "required_entities": [""]},
        {"query": "x", "domains": ["a.fr"] * 21},
    ],
)
def test_request_validation(kwargs):
    with pytest.raises(ValidationError):
        SearchRequest(**kwargs)


def test_obvious_operator_normalization_and_domain_constraint():
    p = policy('BODACC site.bodacc.fr -site:ads.fr "company liquidation"')
    assert p["query"] == 'BODACC site:bodacc.fr -site:ads.fr "company liquidation"'
    assert p["domains"] == ["bodacc.fr"]
    assert p["normalizations"][0]["kind"] == "site_operator"
    accepted, rejected = rank_results(
        [
            result("https://bodacc.fr/a", "BODACC company liquidation"),
            result("https://bodacc.fr.evil.fr/a", "BODACC company liquidation"),
            result("https://sub.bodacc.fr/a", "BODACC company liquidation"),
        ],
        p,
    )
    assert len(accepted) == 2
    assert [x["reason"] for x in rejected] == ["domain_mismatch"]


def test_all_required_entities_match_normalized_boundaries_and_decoded_url():
    p = policy("payment protocol", required_entities=["X402", "Café"])
    accepted, rejected = rank_results(
        [
            result("https://payments.org/Caf%C3%A9/x402", "payment protocol"),
            result("https://payments.org/cafe/x4020", "payment protocol café"),
            result(title="payment protocol x402"),
        ],
        p,
    )
    assert len(accepted) == 1
    assert [x["reason"] for x in rejected] == ["required_entity_missing"] * 2


def test_generic_dataset_results_do_not_replace_query_identifiers():
    accepted, rejected = rank_results(
        [
            result(title="financial dataset insolvency evidence"),
            result(title="BODACC insolvency evidence"),
        ],
        policy("BODACC financial dataset insolvency evidence"),
    )
    assert [x["title"] for x in accepted] == ["BODACC insolvency evidence"]
    assert rejected[0]["reason"] == "query_identifier_missing"
    assert not rank_results(
        [result(title="payment protocol guide")], policy("x402 payment protocol")
    )[0]


def test_each_result_requires_multiple_meaningful_terms_even_with_one_engine():
    accepted, rejected = rank_results(
        [
            result(title="company insolvency records"),
            result("https://records.fr/noise", "company holiday photos"),
        ],
        policy("company insolvency records"),
    )
    assert len(accepted) == 1
    assert rejected[0]["reason"] == "insufficient_topic_match"


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/a",
        "https://localhost/a",
        "https://127.0.0.1/a",
        "https://[::1]/a",
        "https://192.168.0.1/a",
        "https://user:pass@records.fr/a",
        "https://example.com/a",
        "https://www.example.org/a",
        "https://records.test/a",
        "https://bad_host.fr/a",
        "https://-bad.fr/a",
        "https://bad-.fr/a",
        "https://a..fr/a",
        "https://records.fr:wrong/a",
    ],
)
def test_invalid_urls_rejected_without_global_canonical_changes(url):
    accepted, rejected = rank_results([result(url)], policy("BODACC insolvency"))
    assert not accepted
    assert rejected[0]["reason"] == "invalid_public_url"


def test_provider_score_weak_and_dedup_merges_provenance():
    accepted, rejected = rank_results(
        [
            result("https://records.fr/a?utm_source=test", "BODACC insolvency", score=10000),
            result("https://records.fr/a", "BODACC insolvency evidence", engine="bing"),
            result("https://records.fr/b", "BODACC insolvency", score=10000),
        ],
        policy("BODACC insolvency evidence"),
    )
    assert not rejected
    assert accepted[0]["url"] == "https://records.fr/a"
    assert accepted[0]["engines"] == ["duckduckgo", "bing"]
    assert accepted[0]["quality_score"] > accepted[1]["quality_score"]


def test_quoted_phrase_is_strict():
    accepted, rejected = rank_results(
        [
            result(title="BODACC company liquidation"),
            result(title="BODACC company voluntary liquidation"),
        ],
        policy('BODACC "company liquidation"'),
    )
    assert len(accepted) == 1
    assert rejected[0]["reason"] == "quoted_term_missing"
    literal = policy('"site.bodacc.fr" "site:records.fr"')
    assert literal["query"] == '"site.bodacc.fr" "site:records.fr"'
    assert literal["domains"] == []


def test_refinement_preserves_hard_constraints_identifiers_and_names():
    p = policy(
        'please find information about BODACC x402 2026 Lebedev site:records.fr -ads "company liquidation"'
    )
    assert refined_query(p) == 'BODACC x402 2026 Lebedev site:records.fr -ads "company liquidation"'
    assert refined_query(policy("BODACC x402 2026 Lebedev")) is None
    assert refined_query(policy('"please find" BODACC')) is None
    assert refined_query(policy("Find Search Information BODACC")) is None
    assert refined_query(policy("please find x402", required_entities=["find"])) == "find x402"


def test_generic_only_query_still_requires_topic_matches():
    accepted, rejected = rank_results([result(title="holiday photos")], policy("financial dataset"))
    assert not accepted
    assert rejected[0]["reason"] == "insufficient_topic_match"


def test_individual_company_report_matches_multi_company_query():
    p = policy("представитель владельцев облигаций годовые отчеты 2025 АВАНГАРД Капитал РЕГИОН")
    accepted, rejected = rank_results(
        [
            result(
                "https://www.avangard.ru/rus/about/inforaskr/",
                "Раскрытие информации",
                content="АВАНГАРД БАНК годовые отчеты представителя владельцев облигаций 2025",
            ),
            result(
                "https://reports.ru/annual",
                "годовые отчеты представителя владельцев облигаций 2025",
            ),
        ],
        p,
    )
    assert len(accepted) == 1
    assert "query_entity_match" in accepted[0]["reasons"]
    assert rejected[0]["reason"] == "query_identifier_missing"
    # A free-text date ranks a newer primary report without making it mandatory.
    assert rank_results(
        [result(title="BODACC insolvency evidence")], policy("BODACC insolvency evidence 2025")
    )[0]


def test_structured_domains_intersect_site_hierarchies():
    assert policy("reports site:jetlend.ru", domains=["www.jetlend.ru"])["domains"] == [
        "www.jetlend.ru"
    ]
    assert policy("reports site:docs.jetlend.ru", domains=["jetlend.ru"])["domains"] == [
        "docs.jetlend.ru"
    ]
    assert policy("reports site:jetlend.ru OR site:potok.digital", domains=["jetlend.ru"])[
        "domains"
    ] == ["jetlend.ru"]
    with pytest.raises(ValueError, match="disjoint"):
        policy("reports site:jetlend.ru", domains=["potok.digital"])
    with pytest.raises(ValueError, match="disjoint"):
        policy("reports site:jetlend.ru.evil.com", domains=["jetlend.ru"])


def test_unicode_public_hostname_matches_idna_domain_constraint():
    accepted, rejected = rank_results(
        [
            result("https://www.пример.рф/report", "BODACC insolvency evidence"),
            result("https://www.пример.рф.evil.com/report", "BODACC insolvency evidence"),
        ],
        policy("BODACC insolvency", domains=["пример.рф"]),
    )
    assert len(accepted) == 1
    assert accepted[0]["url"] == "https://www.пример.рф/report"
    assert rejected[0]["reason"] == "domain_mismatch"


@pytest.mark.parametrize(
    "field,value",
    [
        ("category", "x" * 101),
        ("language", "x" * 31),
        ("engines", "x" * 501),
        ("time_range", "x" * 51),
        ("domains", ["x" * 254]),
        ("required_entities", ["x" * 201]),
    ],
)
def test_input_strings_bounded(field, value):
    with pytest.raises(ValidationError):
        SearchRequest(query="reports", **{field: value})
    assert SearchRequest(query="reports").max_results == 8
