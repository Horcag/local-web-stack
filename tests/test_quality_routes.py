"""Regression cases for the installed crawler's missing internal search route."""

from copy import deepcopy

import pytest

from scripts.quality.compose_routes import ROUTES, validate_routes


@pytest.fixture
def graph():
    return {
        "services": {
            "searxng": {
                "container_name": "search-instance",
                "ports": [{"target": 8080, "published": "8088"}],
            },
            "crawl4ai": {
                "container_name": "reader-instance",
                "ports": [{"target": 11235, "published": "11236"}],
                "environment": {"SEARXNG_URL": "http://search-instance:8080"},
            },
            "mcp": {
                "environment": {
                    "SEARXNG_URL": "http://searxng:8080",
                    "CRAWL4AI_URL": "http://reader-instance:11235",
                },
            },
        }
    }


def test_valid_service_and_container_name_routes(graph):
    validate_routes(graph)
    alternate = deepcopy(graph)
    alternate["services"]["crawl4ai"]["environment"]["SEARXNG_URL"] = "http://searxng:8080"
    alternate["services"]["mcp"]["environment"]["CRAWL4AI_URL"] = "http://crawl4ai:11235"
    validate_routes(alternate)


@pytest.mark.parametrize("caller,variable,target", ROUTES)
@pytest.mark.parametrize("failure", ["missing", "localhost", "published-port"])
def test_reject_broken_cross_service_routes(graph, caller, variable, target, failure):
    environment = graph["services"][caller]["environment"]
    if failure == "missing":
        environment.pop(variable)
    elif failure == "localhost":
        internal = graph["services"][target]["ports"][0]["target"]
        environment[variable] = f"http://127.0.0.1:{internal}"
    else:
        published = graph["services"][target]["ports"][0]["published"]
        environment[variable] = f"http://{target}:{published}"
    with pytest.raises(ValueError, match=f"{caller}.{variable}"):
        validate_routes(graph)
