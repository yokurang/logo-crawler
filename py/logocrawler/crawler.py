import asyncio
import aiohttp
import re
import logging
import json
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse
import time
import pytest

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("py/logocrawler/logs.txt", mode="w"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Priority": "u=0, i",
}


class LogoCrawler:
    def __init__(
        self, domains, max_retry=3, concurrent_workers=10, min_delay=1.0
    ) -> None:
        self.domains = domains
        self.max_retry = max_retry
        self.results = []
        self.concurrent_workers = concurrent_workers
        self.semaphore = asyncio.Semaphore(concurrent_workers)
        self.work_queue = asyncio.Queue()
        self.min_delay = min_delay
        self.total_domains = len(domains)
        self.completed = 0
        self.successes = 0
        self.failures = 0

    async def populate_work_queue(self):
        logger.info("Populating work queue with domains...")
        for domain in self.domains:
            await self.work_queue.put(
                {
                    "domain": domain,
                    "retry": 1,
                    "last_attempt_timestamp": None,
                    "attempts": [],
                }
            )
        logger.info(f"Added {self.total_domains} domains to the queue.")

    @staticmethod
    def standardise_url(domain, url):
        if not url or url.startswith("data:image"):
            return None
        full_url = urljoin(f"https://{domain}", url)
        parsed = urlparse(full_url)
        stripped = parsed._replace(query="", fragment="")
        return urlunparse(stripped)

    def extract_jsonld_logo(self, domain, scripts):
        def extract_logo_nested(entry):
            if isinstance(entry, str):
                return entry
            elif isinstance(entry, dict):
                for key, value in entry.items():
                    if key.lower() == "logo":
                        result = extract_logo_nested(value)
                        if result:
                            return result
                    elif key.lower() == "url" and isinstance(value, str):
                        return value
                    else:
                        result = extract_logo_nested(value)
                        if result:
                            return result
            elif isinstance(entry, list):
                for item in entry:
                    result = extract_logo_nested(item)
                    if result:
                        return result
            return None

        for script in scripts:
            try:
                data = json.loads(script.string)
                candidates = data if isinstance(data, list) else [data]
                for entry in candidates:
                    logo = extract_logo_nested(entry)
                    if isinstance(logo, str):
                        return self.standardise_url(domain, logo)
            except Exception as e:
                logger.debug(f"[{domain}] JSON-LD parse error: {e}")
        return None

    def extract_favicon(self, domain, elements):
        hrefs = list({el["href"] for el in elements if el.has_attr("href")})
        pngs = [h for h in hrefs if "png" in h.lower()]
        icos = [h for h in hrefs if "ico" in h.lower()]
        for group in [pngs, icos, hrefs]:
            if group:
                return self.standardise_url(domain, max(group, key=len))
        return None

    def fallback_favicon(self, domain):
        return f"https://{domain}/favicon.ico"

    async def fetch_logo(self, session, domain):
        await asyncio.sleep(self.min_delay)
        try:
            logger.info(f"[{domain}] Fetching HTML page...")
            start_time = time.time()
            async with session.get(
                f"https://{domain}",
                headers=HEADERS,
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                scripts = soup.find_all("script", type="application/ld+json")
                links = soup.find_all("link", rel=re.compile("icon", re.I))

                jsonld_logo = self.extract_jsonld_logo(domain, scripts)
                favicon_logo = self.extract_favicon(domain, links)
                fallback_logo = self.fallback_favicon(domain)

                logo = jsonld_logo or favicon_logo or fallback_logo
                source = (
                    "jsonld_logo"
                    if jsonld_logo
                    else "favicon_logo" if favicon_logo else "fallback_logo"
                )

                roundtrip = time.time() - start_time
                logger.info(
                    f"[{domain}] Logo fetched successfully ({source}) in {round(roundtrip, 2)}s"
                )

                return {
                    "domain": domain,
                    "logo": logo,
                    "status": "Success",
                    "jsonld_logo": jsonld_logo,
                    "favicon_logo": favicon_logo,
                    "fallback_logo": fallback_logo,
                    "source": source,
                    "roundtrip": roundtrip,
                }
        except Exception as e:
            logger.warning(f"[{domain}] Fetch failed: {e}")
            return {
                "domain": domain,
                "logo": "",
                "status": "Failure",
                "jsonld_logo": None,
                "favicon_logo": None,
                "fallback_logo": self.fallback_favicon(domain),
                "source": "none",
                "roundtrip": None,
                "error": str(e),
            }

    async def worker(self):
        async with aiohttp.ClientSession() as session:
            while True:
                task = await self.work_queue.get()
                domain = task["domain"]
                retries = task["retry"]
                last_attempt = task["last_attempt_timestamp"]
                now = time.time()

                if last_attempt:
                    delay = 2**retries
                    sleep_duration = max(0, delay - (now - last_attempt))
                    if sleep_duration > 0:
                        logger.info(
                            f"[{domain}] Sleeping for {round(sleep_duration, 2)}s before retry..."
                        )
                        await asyncio.sleep(sleep_duration)

                task["last_attempt_timestamp"] = time.time()
                attempt_start = time.time()

                async with self.semaphore:
                    result = await self.fetch_logo(session, domain)

                attempt_duration = time.time() - attempt_start
                if result["status"] == "Success":
                    task.setdefault("attempts", []).append((attempt_duration, None))
                else:
                    task.setdefault("attempts", []).append(
                        (attempt_duration, result.get("error", "Unknown error"))
                    )

                result["attempts"] = task["attempts"]

                if result["status"] == "Success":
                    logger.info(
                        f"[{domain}] Successfully retrieved logo:\n{json.dumps(result, indent=2)}"
                    )
                    self.results.append(result)
                    self.completed += 1
                    self.successes += 1
                    logger.info(
                        f"[{domain}] Progress: {self.completed}/{self.total_domains} domains completed. ({self.successes} successes, {self.failures} failures)"
                    )
                elif retries < self.max_retry:
                    logger.info(
                        f"[{domain}] Retrying... Attempt {retries + 1}/{self.max_retry}"
                    )
                    await self.work_queue.put(
                        {
                            "domain": domain,
                            "retry": retries + 1,
                            "last_attempt_timestamp": time.time(),
                            "attempts": task["attempts"],
                        }
                    )
                else:
                    logger.error(
                        f"[{domain}] Max retries exceeded. Giving up.\n{json.dumps(result, indent=2)}"
                    )
                    self.results.append(result)
                    self.completed += 1
                    self.failures += 1
                    logger.info(
                        f"[{domain}] Progress: {self.completed}/{self.total_domains} domains completed. ({self.successes} successes, {self.failures} failures)"
                    )

                self.work_queue.task_done()

    async def run(self):
        logger.info("Starting crawler...")
        await self.populate_work_queue()
        tasks = [
            asyncio.create_task(self.worker()) for _ in range(self.concurrent_workers)
        ]
        await self.work_queue.join()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Crawling complete.")
        return self.results


async def crawl_with_queue(domains, min_delay=1.0):
    crawler = LogoCrawler(domains, min_delay=min_delay)
    return await crawler.run()


# Tests
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
