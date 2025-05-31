"""
This is the entry point script for the asynchronous logo crawler.

It handles:
- Loading domain names from standard input
- Launching the asynchronous logo crawling process
  via 'crawl_with_queue(domains)'
- Summarizing crawl results (success rates, timing metrics, saving logs to
  'py/logocrawler/logs.txt')
- Outputting results to standard output

The crawl uses an asyncio queue-based architecture with worker coroutines
that extract logos via a prioritized strategy:
1. JSON-LD logo
2. Meta or image tag with logo-related hints
3. Favicon
"""

import asyncio
import logging
import csv
import sys
import time
from io import StringIO
from crawler import (
    crawl_with_queue,
    LOGO_FOUND,
    FAVICON_FOUND,
    NOTHING_FOUND,
    FAILURE_MSG,
)

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


def load_domains_from_stdin():
    return [
        line.strip() for line in sys.stdin.read().strip().split("\n") if line.strip()
    ]


def summarize(results):
    total = len(results)
    success = sum(1 for r in results if r["status"] in {LOGO_FOUND, FAVICON_FOUND})
    fail = sum(1 for r in results if r["status"] in {NOTHING_FOUND, FAILURE_MSG})

    jsonld = sum(1 for r in results if r["status"] == LOGO_FOUND)
    favicon = sum(1 for r in results if r["status"] == FAVICON_FOUND)
    nothing = sum(1 for r in results if r["status"] == NOTHING_FOUND)
    failure = sum(1 for r in results if r["status"] == FAILURE_MSG)

    all_times = [
        (sum(d for d, _ in r.get("attempts", [])), r["domain"])
        for r in results
        if r.get("attempts")
    ]
    total_time = sum(t for t, _ in all_times)
    avg_time = total_time / len(all_times) if all_times else 0
    max_time, max_domain = max(all_times, default=(0, ""))

    logger.info("\n[SUMMARY]")
    logger.info(f"Total: {total}, Success: {success}, Failure: {fail}")
    logger.info(f"JSON-LD logos: {jsonld}")
    logger.info(f"Favicon logos: {favicon}")
    logger.info(f"Nothing found: {nothing}")
    logger.info(f"Failed fetches: {failure}")
    logger.info(
        f"""Total Roundtrip Time: {total_time:.2f}s,
        Average: {avg_time:.2f}s"""
    )
    logger.info(f"Max Roundtrip Time: {max_time:.2f}s (Domain: {max_domain})")


def write_csv_stdout(results):
    """
    Writes crawl results to standard output in CSV format.

    Output Columns:
    - domain: The domain that was crawled (e.g. "example.com")
    - logo: The final resolved URL of the logo (if any), otherwise an empty string
    - label: The crawl result status, one of:
        - "JSONLD_LOGO_FOUND" (logo found in JSON-LD structured data)
        - "FAVICON_FOUND" (logo found via <link rel="icon"> or similar)
        - "NOTHING_FOUND" (no logo found)
        - "FAILURE" (failed to fetch or parse page)
    - error: Error message if the status is "FAILURE", otherwise empty string
    """
    keys = ["domain", "logo", "label", "error"]
    writer = csv.DictWriter(sys.stdout, fieldnames=keys)
    writer.writeheader()
    for row in results:
        label = row["status"]
        writer.writerow(
            {
                "domain": row["domain"],
                "logo": row["logo"],
                "label": label,
                "error": (row.get("error", "") if row["status"] == FAILURE_MSG else ""),
            }
        )


# tests
def test_load_domains_from_stdin():
    sys.stdin = StringIO("x.com\ny.com\n")
    assert load_domains_from_stdin() == ["x.com", "y.com"]
    sys.stdin = sys.__stdin__  # Reset stdin


def test_write_csv():
    test_data = [
        {
            "domain": "a.com",
            "logo": "a.png",
            "status": LOGO_FOUND,
            "jsonld_logo": "a.png",
            "favicon_logo": None,
            "source": "jsonld_logo",
        },
        {
            "domain": "b.com",
            "logo": "b.png",
            "status": FAVICON_FOUND,
            "jsonld_logo": None,
            "favicon_logo": "b.png",
            "source": "favicon_logo",
        },
        {
            "domain": "c.com",
            "logo": "",
            "status": FAILURE_MSG,
            "jsonld_logo": None,
            "favicon_logo": None,
            "source": "none",
            "error": "Timeout",
        },
    ]

    captured_output = StringIO()
    original_stdout = sys.stdout
    sys.stdout = captured_output

    try:
        write_csv_stdout(test_data)
    finally:
        sys.stdout = original_stdout

    output = captured_output.getvalue()
    assert "domain,logo,label,error" in output
    assert f"a.com,a.png,{LOGO_FOUND}," in output
    assert f"b.com,b.png,{FAVICON_FOUND}," in output
    assert f"c.com,,{FAILURE_MSG},Timeout" in output


if __name__ == "__main__":
    logger.info("Running tests for main.py")
    test_load_domains_from_stdin()
    test_write_csv()
    logger.info("The tests for main.py have all passed.")

    start_time = time.time()

    domains = load_domains_from_stdin()
    logger.info(f"Crawling for logos on {len(domains)} websites")

    results = asyncio.run(crawl_with_queue(domains))
    summarize(results)
    write_csv_stdout(results)

    total_time = time.time() - start_time
    logger.info(f"[TOTAL PROGRAM TIME] {total_time:.2f} seconds")
