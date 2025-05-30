import sys
import asyncio
import logging
import argparse
import csv
from crawler import crawl_with_queue

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


def load_domains(source):
    if source:
        with open(source, "r") as f:
            return [line.strip() for line in f if line.strip()]
    else:
        return [
            line.strip()
            for line in sys.stdin.read().strip().split("\n")
            if line.strip()
        ]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Async logo extractor with worker queue"
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="File containing domains (one per line). Defaults to STDIN if omitted.",
    )
    return parser.parse_args()


def summarize(results):
    total = len(results)
    success = sum(1 for r in results if r["status"] == "Success")
    fail = total - success
    jsonld = sum(1 for r in results if r["jsonld_logo"])
    favicon = sum(1 for r in results if r["favicon_logo"])
    fallback = sum(1 for r in results if r["logo"] == r["fallback_logo"])

    # Roundtrip time statistics based on new 'attempts' structure
    all_times = [
        (sum(d for d, _ in r["attempts"]), r["domain"])
        for r in results
        if r.get("attempts")
    ]
    total_time = sum(t for t, _ in all_times)
    avg_time = total_time / len(all_times) if all_times else 0
    max_time, max_domain = max(all_times, default=(0, ""))

    logger.info("\n[SUMMARY]")
    logger.info(f"Total: {total}, Success: {success}, Failure: {fail}")
    logger.info(
        f"Found JSON-LD: {jsonld}, Favicon: {favicon}, Used Fallback: {fallback}"
    )
    logger.info(f"Total Roundtrip Time: {total_time:.2f}s, Average: {avg_time:.2f}s")
    logger.info(f"Max Roundtrip Time: {max_time:.2f}s (Domain: {max_domain})")


def write_csv_stdout(results):
    keys = ["domain", "logo"]
    writer = csv.DictWriter(sys.stdout, fieldnames=keys)
    writer.writeheader()
    for row in results:
        writer.writerow({k: row[k] for k in keys})


if __name__ == "__main__":
    args = parse_args()
    domains = load_domains(args.input)
    logger.info(f"Crawling for logos on {len(domains)} websites")

    results = asyncio.run(crawl_with_queue(domains))
    summarize(results)
    write_csv_stdout(results)
