"""Keep third-party crawler caches and test databases out of the operator profile."""

import os
from hashlib import sha256
import tempfile

_scratch = tempfile.TemporaryDirectory(prefix="local-web-pytest-")
_previous = os.environ.get("CRAWL4_AI_BASE_DIRECTORY")
os.environ["CRAWL4_AI_BASE_DIRECTORY"] = _scratch.name


def pytest_make_parametrize_id(val, argname):
    """Keep oversized-input cases from flooding CI logs and JUnit with payload bytes."""
    if not isinstance(val, (str, bytes)) or len(val) <= 128:
        return None
    encoded = val.encode("utf-8", errors="surrogatepass") if isinstance(val, str) else val
    return f"{argname}-{type(val).__name__}-{len(val)}-{sha256(encoded).hexdigest()[:8]}"


def pytest_unconfigure(config):
    if _previous is None:
        os.environ.pop("CRAWL4_AI_BASE_DIRECTORY", None)
    else:
        os.environ["CRAWL4_AI_BASE_DIRECTORY"] = _previous
    _scratch.cleanup()
