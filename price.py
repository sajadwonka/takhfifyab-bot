"""Digikala price fetching for the price-hunter bot.

Uses Digikala's public read-only endpoints at ``api.digikala.com`` — the same
ones the Digikala web app itself calls (and the same approach the unofficial
``digikala-mcp`` package uses). No API key, no login.

Endpoints used
--------------
* ``GET /product/v1/products/{id}/`` — title, variants (one offer per
  color/size from one seller), ``properties.min_price_in_last_month``.
* ``GET /v1/product/{id}/price-chart/`` — ~30 days of daily buy-box prices
  per variant (Jalali days).

Prices in Digikala's API are in **Rial**; this module converts to Toman
(divide by 10) everywhere it returns a price.

Why Chrome impersonation?
--------------------------
Digikala sits behind a CDN (ArvanCloud) that drops connections whose TLS
fingerprint does not look like a real browser. Plain ``httpx`` gets its
connection closed without a response; ``httpx-curl-cffi`` with
``impersonate="chrome"`` passes. If the API ever blocks the bot, the
``DIGIKALA_PROXY`` env var (an HTTP(S) proxy URL, e.g. an Iranian VPS)
is honoured as a fallback route.

Rate limiting: one request per product per check cycle, with a short
pause between requests. Never hammer the API.
"""

from __future__ import annotations

import os
import re
import ssl
from dataclasses import dataclass, field

import httpx
from httpx_curl_cffi import AsyncCurlTransport

API = "https://api.digikala.com"
SITE = "https://www.digikala.com"

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
    "Origin": SITE,
    "x-web-client": "desktop",
    "x-web-client-id": "web",
    "Referer": f"{SITE}/",
}

# Pause between consecutive upstream requests (seconds).
REQUEST_DELAY = 0.8
REQUEST_TIMEOUT = 25.0

# digikala.com/product/dkp-12345678/ — also matches bare dkp-12345678
PRODUCT_LINK_RE = re.compile(r"dkp-(\d{1,10})")
PRODUCT_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?digikala\.com/product/dkp-(\d{1,10})/?"
)

# NOTE on the HTML fallback (investigated 2026-11-25): the public product
# page (www.digikala.com/product/dkp-<id>/) is a ~14 KB Next.js shell; the
# product data is fetched client-side from the same API, so the HTML
# contains no price or title to parse. An HTML fallback is therefore not
# viable, and the API below is the only working price source. If the API
# ever becomes unreachable, the fix is a proxy (DIGIKALA_PROXY), not HTML
# scraping — see README.md.


class PriceError(Exception):
    """Base error for price fetching."""


class ProductNotFound(PriceError):
    """The product id does not exist or is no longer listed."""


class ProductOutOfStock(PriceError):
    """The product exists but has no in-stock seller offer right now."""


class ApiError(PriceError):
    """The Digikala API answered with an error / could not be reached."""


def rial_to_toman(rial: int | None) -> int | None:
    """Digikala API prices are Rial; the bot speaks Toman."""
    if rial is None:
        return None
    return int(rial) // 10


def extract_product_id(text: str) -> int | None:
    """Return the Digikala product id from a message, or None."""
    m = PRODUCT_URL_RE.search(text or "")
    if m:
        return int(m.group(1))
    m = PRODUCT_LINK_RE.search(text or "")
    if m:
        return int(m.group(1))
    return None


def format_toman(amount: int | None) -> str:
    """1,234,567 — thousands separators, Latin digits (as Digikala shows)."""
    if amount is None:
        return "—"
    return f"{amount:,}"


def build_product_url(product_id: int) -> str:
    """Product link, affiliate-tagged when the owner set AFFILIO_ID.

    The exact link format comes from the Digikala Affilio panel after the
    owner registers. Two ways to configure it:

    * ``AFFILIO_URL_TEMPLATE`` — a template with ``{id}`` and ``{aff}``
      placeholders, e.g. the exact link Affilio gives you.
    * ``AFFILIO_ID`` — appended as standard ``utm_source``/``utm_campaign``
      parameters to the plain product URL (harmless tracking params
      Digikala accepts). Used only when no template is set.

    When neither is set, the plain Digikala link is returned.
    """
    affilio_id = (os.environ.get("AFFILIO_ID") or "").strip()
    template = (os.environ.get("AFFILIO_URL_TEMPLATE") or "").strip()
    if template:
        url = template.replace("{id}", str(product_id))
        if "{aff}" in url:
            url = url.replace("{aff}", affilio_id)
        return url
    url = f"{SITE}/product/dkp-{product_id}/"
    if affilio_id:
        url += f"?utm_source=affilio&utm_campaign={affilio_id}"
    return url


@dataclass
class ProductInfo:
    product_id: int
    title: str
    price_toman: int | None  # cheapest in-stock offer, Toman
    seller: str | None = None
    lowest_30d_toman: int | None = None  # from product properties
    in_stock: bool = True
    offers_count: int = 0


@dataclass
class PriceHistory:
    product_id: int
    days: list[tuple[str, int]] = field(default_factory=list)  # (jalali day, Toman)

    @property
    def lowest(self) -> int | None:
        prices = [p for _, p in self.days]
        return min(prices) if prices else None

    @property
    def highest(self) -> int | None:
        prices = [p for _, p in self.days]
        return max(prices) if prices else None

    @property
    def last(self) -> int | None:
        return self.days[-1][1] if self.days else None


def _ssl_context() -> ssl.SSLContext:
    """TLS context honouring the sandbox egress CA bundle when present.

    Some sandboxes terminate TLS at an egress proxy with a local CA
    (``SSL_CERT_FILE``). On a normal VPS this is unset and the default
    system roots are used. Verification is never disabled.
    """
    cafile = (
        os.environ.get("SSL_CERT_FILE")
        or os.environ.get("REQUESTS_CA_BUNDLE")
        or os.environ.get("CURL_CA_BUNDLE")
    )
    if cafile and os.path.exists(cafile):
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


class DigikalaClient:
    """Async client for Digikala's public product endpoints."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "DigikalaClient":
        proxy = (
            os.environ.get("DIGIKALA_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
        )
        transport = AsyncCurlTransport(impersonate="chrome")
        self._client = httpx.AsyncClient(
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            proxy=proxy or None,
            # trust_env=False: the sandbox NO_PROXY list contains bracketed
            # IPv6 entries that httpx cannot parse as URL patterns.
            trust_env=False,
            verify=_ssl_context(),
            transport=transport,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_json(self, path: str) -> dict:
        assert self._client is not None
        try:
            resp = await self._client.get(f"{API}{path}")
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.TimeoutException) as e:
            raise ApiError(f"ارتباط با دیجی‌کالا برقرار نشد: {e}") from e
        if resp.status_code in (403, 429, 503):
            raise ApiError(
                f"دیجی‌کالا درخواست را رد کرد (HTTP {resp.status_code})؛ "
                "ممکن است موقتاً مسدود شده باشیم. کمی بعد دوباره تلاش کن."
            )
        if resp.status_code != 200:
            raise ApiError(f"دیجی‌کالا خطای HTTP {resp.status_code} برگرداند.")
        try:
            return resp.json()
        except ValueError as e:
            raise ApiError("پاسخ دیجی‌کالا قابل خواندن نبود.") from e

    async def fetch_product(self, product_id: int) -> ProductInfo:
        """Current cheapest in-stock price for a product."""
        body = await self._get_json(f"/product/v1/products/{product_id}/")
        product = (body.get("data") or {}).get("product") or {}
        if product.get("is_inactive"):
            raise ProductNotFound("این کالا در دیجی‌کالا پیدا نشد یا دیگر عرضه نمی‌شود.")
        title = product.get("title_fa") or f"کالای {product_id}"
        variants = product.get("variants") or []
        offers = []
        for v in variants:
            price = (v.get("price") or {}).get("selling_price")
            if price:
                offers.append((rial_to_toman(price), (v.get("seller") or {}).get("title")))
        offers.sort(key=lambda o: o[0] if o[0] is not None else 10**18)
        lowest_30d = rial_to_toman(
            (product.get("properties") or {}).get("min_price_in_last_month")
        )
        if not offers:
            # No priced variant at all: either out of stock or a bad id.
            raise ProductOutOfStock("این کالا فعلاً موجود نیست (فروشنده‌ای با قیمت ندارد).")
        price, seller = offers[0]
        return ProductInfo(
            product_id=product_id,
            title=title,
            price_toman=price,
            seller=seller,
            lowest_30d_toman=lowest_30d,
            in_stock=True,
            offers_count=len(offers),
        )

    async def fetch_price_history(self, product_id: int) -> PriceHistory:
        """~30 days of daily prices (cheapest variant each day), in Toman."""
        body = await self._get_json(f"/v1/product/{product_id}/price-chart/")
        data = body.get("data") or {}
        days: list[tuple[str, int]] = []
        for variant in data.get("price_chart") or []:
            for h in variant.get("history") or []:
                if h.get("is_marketable") and h.get("selling_price"):
                    days.append((str(h.get("day")), rial_to_toman(h["selling_price"])))
            if days:
                break  # first variant with history is enough for the bot
        return PriceHistory(product_id=product_id, days=days)

async def fetch_product_resilient(product_id: int) -> ProductInfo:
    """Fetch a product via the API.

    The HTML fallback was investigated and dropped: the public product
    page is a JS shell with no price data in it (see the note at the top
    of this module). If the API is unreachable, set DIGIKALA_PROXY.
    """
    async with DigikalaClient() as client:
        return await client.fetch_product(product_id)


def drop_percent(old_price: int, new_price: int) -> float:
    """Price drop as a percentage of the old price (negative = increase)."""
    if not old_price:
        return 0.0
    return (old_price - new_price) / old_price * 100.0
