"""Organic SERP extraction bound to one owned CDP document."""

from contextlib import suppress
from typing import Any

from service.browser_navigation import wait_for_document


ORGANIC_SELECTOR = "li.serp-item .OrganicTitle-Link, li.serp-item .OrganicTitle a[href]"
ORGANIC_SCRIPT = r"""anchors => anchors.slice(0, 100).flatMap(anchor => {
    const card = anchor.closest('li.serp-item');
    const organic = anchor.closest('.Organic');
    if (!card || !organic || !anchor.getClientRects().length) return [];
    if (card.matches('.serp-item_type_adv, .serp-item_type_ad, [data-ad], [data-advertisement]') ||
        card.querySelector('.OrganicLabel_type_ad, .Label_type_ad, .AdvLabel, [data-ad], [data-advertisement]') ||
        anchor.closest('[data-ad], [data-advertisement]') || anchor.rel.split(/\s+/).includes('sponsored') ||
        Array.from(card.querySelectorAll('.OrganicLabel-Text, .Label')).some(label =>
            /^(реклама|advertisement|ads?)$/i.test(label.innerText.trim()))) return [];
    const title = anchor.innerText.trim();
    if (!title || !anchor.href) return [];
    return [{href: anchor.href, title, source: 'yandex-organic-card',
             selector: 'li.serp-item .Organic .OrganicTitle-Link',
             card_text: card.innerText.slice(0, 2000)}];
})"""


async def read_search(
    runtime: Any, url: str, *, allow_captcha_recovery: bool = False
) -> dict | None:
    """Own only the new tab; the caller supplies the overall timeout."""
    if allow_captcha_recovery:
        raise ValueError("Search extraction does not permit CAPTCHA recovery")
    if not runtime.configured:
        return None
    if getattr(runtime, "_targeted_reads", False):
        from service.cdp_reader import read_cdp

        return await read_cdp(runtime, url, search=True)
    async with runtime._lock:
        tab_id = await runtime._open(url)
        try:
            page = await runtime._page(tab_id)
            await wait_for_document(page)
            watch = runtime._navigation_events.get(tab_id)
            if watch:
                await watch.refresh_status()
            revision = watch.revision if watch else None
            document = watch.document_id if watch else None
            actual_url = page.url
            snapshot = await runtime._snapshot(tab_id, allow_captcha_recovery=False)
            cards = await page.locator(ORGANIC_SELECTOR).evaluate_all(ORGANIC_SCRIPT)
            if watch:
                await watch.refresh_status()
            stable = bool(
                watch
                and watch.native
                and document
                and watch.document_id == document
                and watch.revision == revision
                and not watch.inflight
                and page.url == actual_url == snapshot.get("url")
                and runtime._navigation_status.get(tab_id) == snapshot.get("status_code")
            )
            snapshot["search_results"] = cards if stable else []
            snapshot["search_document"] = {
                "verified": stable,
                "document_id": document,
                "revision": revision,
            }
            if not stable:
                snapshot["status_code"] = None
            return snapshot
        finally:
            page = await runtime._forget(tab_id)
            if page is not None:
                with suppress(Exception):
                    await page.close()
