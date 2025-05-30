import pytest
import asyncio
import re
from bs4 import BeautifulSoup
from crawler import LogoCrawler

# Sample data for testing
test_results = [
    {
        "domain": "example.com",
        "logo": "https://example.com/logo.png",
        "status": "Success",
        "jsonld_logo": "https://example.com/logo.png",
        "favicon_logo": "",
        "fallback_logo": "",
        "attempt_durations": [0.5],
    },
    {
        "domain": "test.com",
        "logo": "https://test.com/favicon.ico",
        "status": "Success",
        "jsonld_logo": "",
        "favicon_logo": "https://test.com/favicon.ico",
        "fallback_logo": "",
        "attempt_durations": [1.2, 0.8],
    },
    {
        "domain": "fail.com",
        "logo": "",
        "status": "Failure",
        "jsonld_logo": "",
        "favicon_logo": "",
        "fallback_logo": "",
        "attempt_durations": [],
    },
]


@pytest.mark.asyncio
async def test_populate_work_queue():
    domains = ["example.com", "test.com"]
    crawler = LogoCrawler(domains)
    await crawler.populate_work_queue()
    assert crawler.work_queue.qsize() == len(domains)


def test_standardise_url():
    crawler = LogoCrawler([])
    domain = "example.com"

    # Already complete
    assert (
        crawler.standardise_url(domain, "https://example.com/logo.png")
        == "https://example.com/logo.png"
    )

    # Starts with //
    assert (
        crawler.standardise_url(domain, "//example.com/logo.png")
        == "https://example.com/logo.png"
    )

    # Relative path
    assert (
        crawler.standardise_url(domain, "/logo.png") == "https://example.com/logo.png"
    )

    # No protocol
    assert crawler.standardise_url(domain, "logo.png") == "https://example.com/logo.png"

    # With query
    assert (
        crawler.standardise_url(domain, "/logo.png?foo=bar")
        == "https://example.com/logo.png"
    )


def test_extract_favicon_1():
    html = """
    <link rel="icon" href="favicon.ico">
    <link rel="icon" href="logo.png">
    <link rel="icon" href="logo2.png?version=1">
    """
    soup = BeautifulSoup(html, "html.parser")
    links = soup.find_all("link", rel=re.compile("icon", re.I))
    crawler = LogoCrawler([])
    url = crawler.extract_favicon("example.com", links)
    assert url.endswith("logo2.png")


def test_extract_favicon_2():
    crawler = LogoCrawler([])
    soup = BeautifulSoup(
        """
        <head>
            <link rel="icon" href="/favicon-32x32.png">
            <link rel="icon" href="/favicon.ico">
        </head>
    """,
        "html.parser",
    )
    icons = soup.find_all("link", rel=re.compile("icon", re.I))
    result = crawler.extract_favicon("example.com", icons)
    assert result == "https://example.com/favicon-32x32.png"


def test_extract_jsonld_logo():
    html = """
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Organization",
      "name": "Example",
      "logo": {
        "@type": "ImageObject",
        "url": "https://example.com/logo.png"
      }
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    crawler = LogoCrawler([])
    logo_url = crawler.extract_jsonld_logo("example.com", scripts)
    assert logo_url == "https://example.com/logo.png"


def test_fallback_favicon():
    crawler = LogoCrawler([])
    assert crawler.fallback_favicon("example.com") == "https://example.com/favicon.ico"


def test_extract_jsonld_logo_nested():
    crawler = LogoCrawler([])
    soup = BeautifulSoup(
        """
        <script type="application/ld+json">
            {
                "@context": "http://schema.org",
                "@type": "Organization",
                "logo": {
                    "url": "https://example.com/nested-logo.png"
                }
            }
        </script>
    """,
        "html.parser",
    )
    scripts = soup.find_all("script", type="application/ld+json")
    result = crawler.extract_jsonld_logo("example.com", scripts)
    assert result == "https://example.com/nested-logo.png"


@pytest.mark.asyncio
async def test_worker_retries(monkeypatch):
    domains = ["retry.com"]
    crawler = LogoCrawler(domains, max_retry=1, concurrent_workers=1)

    call_count = {"count": 0}

    async def fake_fetch(session, domain):
        call_count["count"] += 1
        if call_count["count"] == 1:
            return {
                "domain": domain,
                "logo": "",
                "status": "Failure",
                "jsonld_logo": None,
                "favicon_logo": None,
                "fallback_logo": crawler.fallback_favicon(domain),
                "roundtrip": None,
            }
        return {
            "domain": domain,
            "logo": "https://retry.com/logo.png",
            "status": "Success",
            "jsonld_logo": None,
            "favicon_logo": "https://retry.com/logo.png",
            "fallback_logo": crawler.fallback_favicon(domain),
            "roundtrip": 0.1,
        }

    monkeypatch.setattr(crawler, "fetch_logo", fake_fetch)
    await crawler.run()
    assert call_count["count"] == 2
    assert crawler.results[0]["status"] == "Success"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "domain", ["facebook.com", "twitter.com", "ask.com", "att.com", "go.com"]
)
async def test_fetch_logo_real(domain):
    crawler = LogoCrawler([domain])
    async with asyncio.Semaphore(1):
        async with asyncio.timeout(15):
            async with crawler.semaphore:
                async with asyncio.ClientSession() as session:
                    result = await crawler.fetch_logo(session, domain)
                    assert result["domain"] == domain
                    assert result["status"] in ("Success", "Failure")
                    assert isinstance(result["jsonld_logo"], (str, type(None)))
                    assert isinstance(result["favicon_logo"], (str, type(None)))
                    assert isinstance(result["fallback_logo"], str)
                    if result["status"] == "Success":
                        assert result["logo"]
