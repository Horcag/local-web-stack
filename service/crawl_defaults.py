"""Browser environment defaults and narrowly scoped launcher compatibility patches."""

import importlib
import os
from typing import Literal, cast


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _default_cache_mode() -> Literal["bypass", "enabled"]:
    value = os.environ.get("CRAWL4AI_CACHE_MODE", "enabled").strip().lower()
    return "bypass" if value == "bypass" else "enabled"


def _default_headers() -> dict[str, str]:
    # Patchright counts custom headers as fingerprint injection: an
    # Accept-Language that disagrees with the profile's own locale is a cheap
    # detection signal. An empty env value means "let Chrome speak for itself".
    language = os.environ.get(
        "CRAWL4AI_ACCEPT_LANGUAGE",
        "en-US,en;q=0.9,ru;q=0.8",
    ).strip()
    return {"Accept-Language": language} if language else {}


def _default_user_data_dir() -> str:
    return os.environ.get("CRAWL4AI_USER_DATA_DIR", "/app/work/browser-profile").strip()


_BROWSER_TYPES = {"chromium", "firefox", "webkit", "undetected"}
_PATCHED_LAUNCHERS: set[str] = set()


def _default_browser_type() -> Literal["chromium", "firefox", "webkit", "undetected"]:
    value = os.environ.get("CRAWL4AI_BROWSER_TYPE", "chromium").strip().lower()
    return (
        cast(Literal["chromium", "firefox", "webkit", "undetected"], value)
        if value in _BROWSER_TYPES
        else "chromium"
    )


def _default_chrome_channel() -> str | None:
    # Real Google Chrome rather than Playwright's bundled build. With
    # headless=True Playwright resolves "chromium" to chrome-headless-shell, a
    # stripped binary that is trivially fingerprinted; Patchright's documented
    # best practice is channel="chrome" on a persistent context.
    return os.environ.get("CRAWL4AI_CHROME_CHANNEL", "chrome").strip() or None


def _stripped_flag_prefixes() -> tuple[str, ...]:
    """CLI flags to drop from the browser command line.

    crawl4ai hardcodes --ignore-certificate-errors in build_browser_flags(),
    independently of BrowserConfig.ignore_https_errors, so turning the config
    option off is not enough. The flag has a JS-observable effect (an invalid
    subresource certificate loads instead of failing), which makes it one of
    the few command-line settings a page can actually probe for.
    """
    raw = os.environ.get(
        "CRAWL4AI_STRIP_BROWSER_FLAGS",
        "--ignore-certificate-errors",
    )
    return tuple(flag.strip() for flag in raw.split(",") if flag.strip())


def _install_launch_patches() -> None:
    """Force `channel` and `no_viewport` onto persistent-context launches.

    crawl4ai 0.9.2 hand-builds the kwargs for launch_persistent_context() in
    browser_manager.py and never forwards BrowserConfig.chrome_channel, so the
    system Chrome install is silently ignored (the kwargs are only assembled
    with the channel on the non-persistent path). Patching at the API boundary
    keeps this independent of crawl4ai internals: once a release forwards the
    values itself, its kwargs win.
    """
    channel = _default_chrome_channel()
    no_viewport = _env_bool("CRAWL4AI_NO_VIEWPORT", True)
    stripped = _stripped_flag_prefixes()
    if not channel and not no_viewport and not stripped:
        return

    for module_name in ("patchright.async_api", "playwright.async_api"):
        if module_name in _PATCHED_LAUNCHERS:
            continue
        try:
            browser_type = importlib.import_module(module_name).BrowserType
        except (ImportError, AttributeError):
            continue

        original = browser_type.launch_persistent_context

        async def launch_persistent_context(
            self,
            user_data_dir,
            *args,
            _original=original,
            **kwargs,
        ):
            if channel:
                kwargs.setdefault("channel", channel)
            if no_viewport and "no_viewport" not in kwargs:
                # Playwright rejects viewport and no_viewport together; the
                # fixed viewport is itself a mismatch signal against the real
                # window size, so drop it.
                kwargs.pop("viewport", None)
                kwargs["no_viewport"] = True
            if stripped and kwargs.get("args"):
                kwargs["args"] = [flag for flag in kwargs["args"] if not flag.startswith(stripped)]
            return await _original(self, user_data_dir, *args, **kwargs)

        browser_type.launch_persistent_context = launch_persistent_context
        _PATCHED_LAUNCHERS.add(module_name)


_install_launch_patches()
