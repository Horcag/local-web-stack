"""Keep third-party crawler caches and test databases out of the operator profile."""

import os
import tempfile

_scratch = tempfile.TemporaryDirectory(prefix="local-web-pytest-")
_previous = os.environ.get("CRAWL4_AI_BASE_DIRECTORY")
os.environ["CRAWL4_AI_BASE_DIRECTORY"] = _scratch.name


def pytest_unconfigure(config):
    if _previous is None:
        os.environ.pop("CRAWL4_AI_BASE_DIRECTORY", None)
    else:
        os.environ["CRAWL4_AI_BASE_DIRECTORY"] = _previous
    _scratch.cleanup()
