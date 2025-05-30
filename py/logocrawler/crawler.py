"""
This file implements the LogoCrawler class which crawls a given set of
domains for logos.

The crawler is designed using an asynchronous queue-based architecture.
A queue is populated with domain tasks, and a pool of asynchronous worker
coroutines (bounded by a semaphore to control concurrency) continuously
pulls and processes these tasks.

Each worker fetches the HTML of a domain and attempts to extract a logo using
a fallback strategy:

1. JSON-LD Extraction – Tries to find
   structured data in `<script type="application/ld+json">`
   blocks, searching for known schema patterns that commonly contain
   logo URLs.
2. Favicon Extraction – If no JSON-LD logo is found, attempts
   to find `<link rel="icon">` or similar tags that
   reference `.png`, `.ico`, or other favicon formats.
3. Default Fallback – If all else fails, it falls
   back to using `https://{domain}/favicon.ico`.

All URLs are standardized via `urljoin` to ensure absolute paths.
Retry logic is included for resilience, with exponential backoff.
Results are recorded with timing and source metadata.

The crawl_with_queue(deomains) function can be called to instantiate
a LogoCrawler object and runs the worker pool.
"""

import asyncio
import aiohttp
import re
import logging
import json
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse
import time

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

LOGO_FOUND = "JSONLD_LOGO_FOUND"
NOTHING_FOUND = "NOTHING_FOUND"
FAVICON_FOUND = "FAVICON_FOUND"
FALLBACK_FAVICON_FOUND = "FALLBACK_FAVICON_FOUND"
FAILURE_MSG = "FAILURE"


class LogoCrawler:
    def __init__(self, domains, max_retry=3, min_delay=0.5) -> None:
        self.domains = domains
        self.max_retry = max_retry
        self.results = []
        self.concurrent_workers = max(1, len(domains) // 50)
        self.semaphore = asyncio.Semaphore(self.concurrent_workers)
        self.work_queue = asyncio.Queue()
        self.min_delay = min_delay
        self.total_domains = len(domains)
        self.completed = 0
        self.successes = 0
        self.failures = 0
        self.jsonld_count = 0
        self.favicon_count = 0
        self.fallback_count = 0

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
        if url.startswith("//"):
            url = "https:" + url
        elif "http" not in url and "data:image" not in url:
            if not url.startswith("/"):
                url = "https://" + domain + "/" + url  # for favicons
            else:
                url = "https://" + domain + url
        parsed = urlparse(url)
        cleaned = parsed._replace(query="", fragment="")
        return urlunparse(cleaned)

    def extract_jsonld_logo(self, domain, elements):
        """Extracts logo URL from JSON-LD scripts on a page.
        Logos in JSON-LD are typically represented in the following patterns:
        1. data['logo']
        2. data['logo']['logo']
        3. data['publisher']['logo']['url']
        4. data['@graph'][-1]['logo']
        """

        def extract_logo(data):
            # Case: direct logo string
            if isinstance(data, str):
                return data

            # Case: logo as dictionary with 'url' key
            if isinstance(data, dict):
                if "logo" in data:
                    return extract_logo(data["logo"])
                if "@graph" in data:
                    for entry in data["@graph"]:
                        result = extract_logo(entry)
                        if result:
                            return result
                if "publisher" in data and "logo" in data["publisher"]:
                    return extract_logo(data["publisher"]["logo"])

                if "url" in data:
                    return data["url"]

            # Case: list of objects
            if isinstance(data, list):
                for item in data:
                    result = extract_logo(item)
                    if result:
                        return result

            return None

        for element in elements:
            if not element.string or "logo" not in element.string:
                continue
            try:
                data = json.loads(element.string)
                logo_url = extract_logo(data)
                if logo_url:
                    return self.standardise_url(domain, logo_url)
            except Exception as e:
                logger.debug(f"[{domain}] Failed to parse JSON-LD: {e}")

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

                logo = None
                source = "none"
                status = NOTHING_FOUND

                if jsonld_logo:
                    logo = jsonld_logo
                    source = "jsonld_logo"
                    status = LOGO_FOUND
                elif favicon_logo:
                    logo = favicon_logo
                    source = "favicon_logo"
                    status = FAVICON_FOUND
                elif fallback_logo:
                    logo = fallback_logo
                    source = "fallback_logo"
                    status = FALLBACK_FAVICON_FOUND

                roundtrip = time.time() - start_time

                if logo:
                    logger.info(
                        f"""[{domain}] Logo fetched successfully
                        ({source}) in {round(roundtrip, 2)}s"""
                    )
                    return {
                        "domain": domain,
                        "logo": logo,
                        "status": status,
                        "jsonld_logo": jsonld_logo,
                        "favicon_logo": favicon_logo,
                        "fallback_logo": fallback_logo,
                        "source": source,
                        "roundtrip": roundtrip,
                    }
                else:
                    logger.warning(f"[{domain}] No logo found by any strategy")
                    return {
                        "domain": domain,
                        "logo": "",
                        "status": NOTHING_FOUND,
                        "jsonld_logo": None,
                        "favicon_logo": None,
                        "fallback_logo": None,
                        "source": "none",
                        "roundtrip": None,
                        "error": NOTHING_FOUND,
                    }

        except Exception as e:
            logger.warning(f"[{domain}] Fetch failed: {e}")
            return {
                "domain": domain,
                "logo": "",
                "status": FAILURE_MSG,
                "jsonld_logo": None,
                "favicon_logo": None,
                "fallback_logo": None,
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
                            f"""[{domain}] Sleeping for
                            {round(sleep_duration, 2)}s before retry..."""
                        )
                        await asyncio.sleep(sleep_duration)

                task["last_attempt_timestamp"] = time.time()
                attempt_start = time.time()

                async with self.semaphore:
                    try:
                        result = await self.fetch_logo(session, domain)
                    except Exception as e:
                        logger.warning(
                            f"[{domain}] Unhandled exception during fetch: {e}"
                        )
                        result = {
                            "domain": domain,
                            "logo": "",
                            "status": FAILURE_MSG,
                            "jsonld_logo": None,
                            "favicon_logo": None,
                            "fallback_logo": None,
                            "source": "none",
                            "roundtrip": None,
                            "error": str(e),
                        }

                attempt_duration = time.time() - attempt_start
                if result["status"] in {
                    LOGO_FOUND,
                    FAVICON_FOUND,
                    FALLBACK_FAVICON_FOUND,
                }:
                    task.setdefault("attempts", []).append((attempt_duration, None))
                else:
                    task.setdefault("attempts", []).append(
                        (attempt_duration, result.get("error", "Unknown error"))
                    )

                result["attempts"] = task["attempts"]
                status = result["status"]

                if status in {LOGO_FOUND, FAVICON_FOUND, FALLBACK_FAVICON_FOUND}:
                    logger.info(
                        f"""[{domain}] Successfully retrieved logo:
                        \n{json.dumps(result, indent=2)}"""
                    )
                    self.results.append(result)
                    self.completed += 1
                    self.successes += 1

                    if status == LOGO_FOUND:
                        self.jsonld_count += 1
                    elif status == FAVICON_FOUND:
                        self.favicon_count += 1
                    elif status == FALLBACK_FAVICON_FOUND:
                        self.fallback_count += 1

                    logger.info(
                        f"[{domain}] Progress: {self.completed}/{self.total_domains} domains completed. "
                        f"({self.successes} successes, {self.failures} failures) | "
                        f"JSON-LD: {self.jsonld_count}, Favicon: {self.favicon_count}, Fallback: {self.fallback_count}"
                    )

                elif status == NOTHING_FOUND and retries < self.max_retry:
                    logger.info(
                        f"""[{domain}] Retrying... Attempt
                        {retries + 1}/{self.max_retry}"""
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
                    # Covers both FAILURE_MSG and maxed out NOTHING_FOUND
                    logger.error(
                        f"""[{domain}] Max retries exceeded or unrecoverable
                        error. Giving up.\n{json.dumps(result, indent=2)}"""
                    )
                    self.results.append(result)
                    self.completed += 1
                    self.failures += 1
                    logger.info(
                        f"[{domain}] Progress: {self.completed}/{self.total_domains} domains completed. "
                        f"({self.successes} successes, {self.failures} failures) | "
                        f"JSON-LD: {self.jsonld_count}, Favicon: {self.favicon_count}, Fallback: {self.fallback_count}"
                    )

                self.work_queue.task_done()

    async def run(self):
        logger.info(f"Starting crawler with {self.concurrent_workers} workers...")
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
# ChatGPT was used to help generate test cases
def test_standardise_url():
    print("test_standardise_url")
    crawler = LogoCrawler([])
    domain = "example.com"

    actual = crawler.standardise_url(domain, "https://example.com/logo.png")
    print("Actual:", actual, "| Expected:", "https://example.com/logo.png")
    assert actual == "https://example.com/logo.png"

    actual = crawler.standardise_url(domain, "//example.com/logo.png")
    print("Actual:", actual, "| Expected:", "https://example.com/logo.png")
    assert actual == "https://example.com/logo.png"

    actual = crawler.standardise_url(domain, "/logo.png")
    print("Actual:", actual, "| Expected:", "https://example.com/logo.png")
    assert actual == "https://example.com/logo.png"

    actual = crawler.standardise_url(domain, "logo.png")
    print("Actual:", actual, "| Expected:", "https://example.com/logo.png")
    assert actual == "https://example.com/logo.png"

    actual = crawler.standardise_url(domain, "/logo.png?foo=bar")
    print("Actual:", actual, "| Expected:", "https://example.com/logo.png")
    assert actual == "https://example.com/logo.png"


def test_extract_favicon_1():
    print("test_extract_favicon_1")
    html = """
    <link rel="icon" href="favicon.ico">
    <link rel="icon" href="logo.png">
    <link rel="icon" href="logo2.png?version=1">
    """
    soup = BeautifulSoup(html, "html.parser")
    links = soup.find_all("link", rel=re.compile("icon", re.I))
    crawler = LogoCrawler([])
    url = crawler.extract_favicon("example.com", links)
    print("Actual:", url, "| Expected to end with:", "logo2.png")
    assert url.endswith("logo2.png")


def test_extract_favicon_2():
    print("test_extract_favicon_2")
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
    print("Actual:", result, "| Expected:", "https://example.com/favicon-32x32.png")
    assert result == "https://example.com/favicon-32x32.png"


def test_extract_jsonld_logo_1():
    print("test_extract_jsonld_logo_1")
    html = """
    <script type="application/ld+json">
    {
      "logo": "https://example.com/logo.png"
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", "https://example.com/logo.png")
    assert result == "https://example.com/logo.png"


def test_extract_jsonld_logo_2():
    print("test_extract_jsonld_logo_2")
    html = """
    <script type="application/ld+json">
    {
      "logo": { "url": "https://example.com/logo2.png" }
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", "https://example.com/logo2.png")
    assert result == "https://example.com/logo2.png"


def test_extract_jsonld_logo_3():
    print("test_extract_jsonld_logo_3")
    html = """
    <script type="application/ld+json">
    {
      "publisher": { "logo": { "url": "https://example.com/logo3.png" } }
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", "https://example.com/logo3.png")
    assert result == "https://example.com/logo3.png"


def test_extract_jsonld_logo_4():
    print("test_extract_jsonld_logo_4")
    html = """
    <script type="application/ld+json">
    {
      "logo": { "logo": { "url": "https://example.com/logo4.png" } }
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", "https://example.com/logo4.png")
    assert result == "https://example.com/logo4.png"


def test_extract_jsonld_logo_5():
    print("test_extract_jsonld_logo_5")
    html = """
    <script type="application/ld+json">
    [
      { "logo": "https://example.com/logo5.png" }
    ]
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", "https://example.com/logo5.png")
    assert result == "https://example.com/logo5.png"


def test_extract_jsonld_logo_6():
    print("test_extract_jsonld_logo_6")
    html = """
    <script type="application/ld+json">
    {
      "publisher": { "logo": "https://example.com/logo6.png" }
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", "https://example.com/logo6.png")
    assert result == "https://example.com/logo6.png"


def test_extract_jsonld_logo_7():
    print("test_extract_jsonld_logo_7")
    html = """
    <script type="application/ld+json">
    { "logo": "/static/logo7.svg" }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", "https://example.com/static/logo7.svg")
    assert result == "https://example.com/static/logo7.svg"


def test_extract_jsonld_logo_8():
    print("test_extract_jsonld_logo_8")
    html = """
    <script type="application/ld+json">
    {
      "logo": { "url": "logo8.png" }
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", "https://example.com/logo8.png")
    assert result == "https://example.com/logo8.png"


def test_extract_jsonld_logo_9():
    print("test_extract_jsonld_logo_9")
    html = """
    <script type="application/ld+json">
    {
      "logo": null
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", None)
    assert result is None


def test_extract_jsonld_logo_10():
    print("test_extract_jsonld_logo_10")
    html = """
    <script type="application/ld+json">
    { "name": "NoLogoSite" }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", None)
    assert result is None


def test_extract_jsonld_logo_11():
    print("test_extract_jsonld_logo_nested_1")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        { "@type": "Thing" },
        {
          "@type": "Organization",
          "logo": {
            "url": "https://example.com/logo1.png"
          }
        }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", "https://example.com/logo1.png")
    assert result == "https://example.com/logo1.png"


def test_extract_jsonld_logo_12():
    print("test_extract_jsonld_logo_nested_2")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        {
          "name": "Company",
          "publisher": {
            "logo": {
              "url": "https://example.com/logo2.png"
            }
          }
        }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", "https://example.com/logo2.png")
    assert result == "https://example.com/logo2.png"


def test_extract_jsonld_logo_13():
    print("test_extract_jsonld_logo_nested_3")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        {
          "logo": {
            "logo": {
              "url": "https://example.com/logo3.png"
            }
          }
        }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", "https://example.com/logo3.png")
    assert result == "https://example.com/logo3.png"


def test_extract_jsonld_logo_14():
    print("test_extract_jsonld_logo_nested_4")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        { "logo": "https://example.com/logo4.png" }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", "https://example.com/logo4.png")
    assert result == "https://example.com/logo4.png"


def test_extract_jsonld_logo_15():
    print("test_extract_jsonld_logo_nested_5")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        {
          "name": "Something",
          "logo": {
            "@type": "ImageObject",
            "url": "/nested/logo5.svg"
          }
        }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", "https://example.com/nested/logo5.svg")
    assert result == "https://example.com/nested/logo5.svg"


def test_extract_jsonld_logo_16():
    print("test_extract_jsonld_logo_nested_6")
    html = """
    <script type="application/ld+json">
    {
      "@graph": []
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", None)
    assert result is None


def test_extract_jsonld_logo_17():
    print("test_extract_jsonld_logo_nested_7")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        {
          "publisher": {
            "logo": "/relative/logo7.png"
          }
        }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", "https://example.com/relative/logo7.png")
    assert result == "https://example.com/relative/logo7.png"


def test_extract_jsonld_logo_18():
    print("test_extract_jsonld_logo_nested_8")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        {
          "logo": null
        }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", None)
    assert result is None


def test_extract_jsonld_logo_19():
    print("test_extract_jsonld_logo_nested_9")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        {
          "otherField": "value"
        },
        {
          "name": "Company",
          "logo": { "url": "logo9.png" }
        }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", "https://example.com/logo9.png")
    assert result == "https://example.com/logo9.png"


def test_extract_jsonld_logo_20():
    print("test_extract_jsonld_logo_nested_10")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        {
          "logo": {
            "logo": {
              "url": null
            }
          }
        }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", scripts)
    print("Actual:", result, "| Expected:", None)
    assert result is None


def test_extract_jsonld_logo_21():
    print("test_extract_jsonld_logo")
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
    print("Actual:", logo_url, "| Expected:", "https://example.com/logo.png")
    assert logo_url == "https://example.com/logo.png"


def test_extract_jsonld_logo_22():
    print("test_extract_jsonld_logo_nested_22")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        { "logo": "https://example.com/logo1.png" }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", soup.find_all("script"))
    print("Actual:", result, "| Expected:", "https://example.com/logo1.png")
    assert result == "https://example.com/logo1.png"


def test_extract_jsonld_logo_23():
    print("test_extract_jsonld_logo_nested_23")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        {
          "logo": {
            "logo": {
              "url": "https://example.com/logo2.png"
            }
          }
        }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", soup.find_all("script"))
    print("Actual:", result, "| Expected:", "https://example.com/logo2.png")
    assert result == "https://example.com/logo2.png"


def test_extract_jsonld_logo_24():
    print("test_extract_jsonld_logo_nested_24")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        {
          "publisher": {
            "logo": {
              "url": "https://example.com/logo3.png"
            }
          }
        }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", soup.find_all("script"))
    print("Actual:", result, "| Expected:", "https://example.com/logo3.png")
    assert result == "https://example.com/logo3.png"


def test_extract_jsonld_logo_25():
    print("test_extract_jsonld_logo_nested_25")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        { "name": "Other" },
        {
          "logo": {
            "url": "https://example.com/logo4.png"
          }
        }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", soup.find_all("script"))
    print("Actual:", result, "| Expected:", "https://example.com/logo4.png")
    assert result == "https://example.com/logo4.png"


def test_extract_jsonld_logo_26():
    print("test_extract_jsonld_logo_nested_26")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        {
          "logo": {
            "@type": "ImageObject",
            "url": "/media/logo5.png"
          }
        }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", soup.find_all("script"))
    print("Actual:", result, "| Expected:", "https://example.com/media/logo5.png")
    assert result == "https://example.com/media/logo5.png"


def test_extract_jsonld_logo_27():
    print("test_extract_jsonld_logo_nested_27")
    html = """
    <script type="application/ld+json">
    {
      "@graph": []
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", soup.find_all("script"))
    print("Actual:", result, "| Expected:", None)
    assert result is None


def test_extract_jsonld_logo_28():
    print("test_extract_jsonld_logo_nested_28")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        {
          "publisher": {
            "logo": "/images/logo7.svg"
          }
        }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", soup.find_all("script"))
    print("Actual:", result, "| Expected:", "https://example.com/images/logo7.svg")
    assert result == "https://example.com/images/logo7.svg"


def test_extract_jsonld_logo_29():
    print("test_extract_jsonld_logo_nested_29")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        {
          "logo": null
        }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", soup.find_all("script"))
    print("Actual:", result, "| Expected:", None)
    assert result is None


def test_extract_jsonld_logo_30():
    print("test_extract_jsonld_logo_nested_30")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        { "other": "field" },
        {
          "logo": {
            "url": "logo9.png"
          }
        }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", soup.find_all("script"))
    print("Actual:", result, "| Expected:", "https://example.com/logo9.png")
    assert result == "https://example.com/logo9.png"


def test_extract_jsonld_logo_31():
    print("test_extract_jsonld_logo_nested_31")
    html = """
    <script type="application/ld+json">
    {
      "@graph": [
        {
          "logo": {
            "logo": {
              "url": null
            }
          }
        }
      ]
    }
    </script>
    """
    soup = BeautifulSoup(html, "html.parser")
    result = LogoCrawler([]).extract_jsonld_logo("example.com", soup.find_all("script"))
    print("Actual:", result, "| Expected:", None)
    assert result is None


def test_extract_jsonld_logo_32():
    print("test_extract_jsonld_logo_nested")
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
    print("Actual:", result, "| Expected:", "https://example.com/nested-logo.png")
    assert result == "https://example.com/nested-logo.png"


def test_fallback_favicon():
    print("test_fallback_favicon")
    crawler = LogoCrawler([])
    fallback = crawler.fallback_favicon("example.com")
    print("Actual:", fallback, "| Expected:", "https://example.com/favicon.ico")
    assert fallback == "https://example.com/favicon.ico"


def test_worker_retries():
    print("test_worker_retries (retry then success)")
    domains = ["retry.com"]
    crawler = LogoCrawler(domains, max_retry=3)
    call_count = {"count": 0}

    async def fake_fetch(session, domain):
        call_count["count"] += 1
        if call_count["count"] == 1:
            return {
                "domain": domain,
                "logo": "",
                "status": NOTHING_FOUND,
                "jsonld_logo": None,
                "favicon_logo": None,
                "fallback_logo": crawler.fallback_favicon(domain),
                "source": "none",
                "roundtrip": None,
                "error": "Simulated Nothing Found",
            }
        return {
            "domain": domain,
            "logo": "https://retry.com/logo.png",
            "status": FAVICON_FOUND,
            "jsonld_logo": None,
            "favicon_logo": "https://retry.com/logo.png",
            "fallback_logo": crawler.fallback_favicon(domain),
            "source": "favicon_logo",
            "roundtrip": 0.1,
        }

    async def run_test():
        crawler.fetch_logo = fake_fetch
        await crawler.run()
        print("Call count:", call_count["count"], "| Expected:", 2)
        print(
            "Final status:", crawler.results[0]["status"], "| Expected:", FAVICON_FOUND
        )
        assert call_count["count"] == 2
        assert crawler.results[0]["status"] == FAVICON_FOUND

    asyncio.run(run_test())


def test_worker_max_retries_exceeded():
    print("test_worker_max_retries_exceeded (3 failures)")
    domains = ["fail.com"]
    crawler = LogoCrawler(domains, max_retry=3)

    async def always_fail(session, domain):
        return {
            "domain": domain,
            "logo": "",
            "status": NOTHING_FOUND,
            "jsonld_logo": None,
            "favicon_logo": None,
            "fallback_logo": crawler.fallback_favicon(domain),
            "source": "none",
            "roundtrip": None,
            "error": "No logo found",
        }

    async def run_test():
        crawler.fetch_logo = always_fail
        await crawler.run()
        print("Retries attempted:", len(crawler.results[0]["attempts"]))
        print(
            "Final status:", crawler.results[0]["status"], "| Expected:", NOTHING_FOUND
        )
        assert crawler.results[0]["status"] == NOTHING_FOUND
        assert len(crawler.results[0]["attempts"]) == 3

    asyncio.run(run_test())


def test_worker_logo_success_immediate():
    print("test_worker_logo_success_immediate")
    domains = ["logo.com"]
    crawler = LogoCrawler(domains, max_retry=3)

    async def return_jsonld_logo(session, domain):
        return {
            "domain": domain,
            "logo": "https://logo.com/logo.png",
            "status": LOGO_FOUND,
            "jsonld_logo": "https://logo.com/logo.png",
            "favicon_logo": None,
            "fallback_logo": crawler.fallback_favicon(domain),
            "source": "jsonld_logo",
            "roundtrip": 0.1,
        }

    async def run_test():
        crawler.fetch_logo = return_jsonld_logo
        await crawler.run()
        assert crawler.results[0]["status"] == LOGO_FOUND
        assert crawler.successes == 1
        assert crawler.failures == 0

    asyncio.run(run_test())


def test_worker_fetch_error():
    print("test_worker_fetch_error")
    domains = ["error.com"]
    crawler = LogoCrawler(domains, max_retry=1)

    async def raise_exception(session, domain):
        raise Exception("Network failure")

    async def run_test():
        crawler.fetch_logo = raise_exception
        await crawler.run()
        print("Status:", crawler.results[0]["status"], "| Expected:", FAILURE_MSG)
        assert crawler.results[0]["status"] == FAILURE_MSG
        assert crawler.failures == 1
        assert crawler.successes == 0

    asyncio.run(run_test())


if __name__ == "__main__":
    print("Running tests in py/logocrawler/crawler.py...")
    test_standardise_url()
    test_extract_favicon_1()
    test_extract_favicon_2()
    test_extract_jsonld_logo_1()
    test_extract_jsonld_logo_2()
    test_extract_jsonld_logo_3()
    test_extract_jsonld_logo_4()
    test_extract_jsonld_logo_5()
    test_extract_jsonld_logo_6()
    test_extract_jsonld_logo_7()
    test_extract_jsonld_logo_8()
    test_extract_jsonld_logo_9()
    test_extract_jsonld_logo_10()
    test_extract_jsonld_logo_11()
    test_extract_jsonld_logo_12()
    test_extract_jsonld_logo_13()
    test_extract_jsonld_logo_14()
    test_extract_jsonld_logo_15()
    test_extract_jsonld_logo_16()
    test_extract_jsonld_logo_17()
    test_extract_jsonld_logo_18()
    test_extract_jsonld_logo_19()
    test_extract_jsonld_logo_20()
    test_extract_jsonld_logo_21()
    test_extract_jsonld_logo_22()
    test_extract_jsonld_logo_23()
    test_extract_jsonld_logo_24()
    test_extract_jsonld_logo_25()
    test_extract_jsonld_logo_26()
    test_extract_jsonld_logo_27()
    test_extract_jsonld_logo_28()
    test_extract_jsonld_logo_29()
    test_extract_jsonld_logo_30()
    test_extract_jsonld_logo_31()
    test_extract_jsonld_logo_32()
    test_fallback_favicon()
    test_worker_retries()
    test_worker_max_retries_exceeded()
    test_worker_logo_success_immediate()
    test_worker_fetch_error()
    print("All tests completed.")
