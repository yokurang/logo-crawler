# LogoCrawler System Design

This document describes the system design of my logo crawler project.

---

## Overview

LogoCrawler is a Python-based asynchronous web crawler designed to extract logo URLs from a list of domain names. It reads domain names from standard input (STDIN) and outputs the results to standard output (STDOUT) as a CSV file containing the columns `domain,logo,label,error`. The column `label` labels what kind of url was found - either a logo, favicon, nothing, or an error. Lastly, if an error occurred, we attach the error message to the result. Otherwise, record is an empty string. Note that the priority with regards which result to return is 1) a logo from json-ld, 2) a pngs or jpegs, and finally 3) a favicon.

---

## Architecture

Tasks are processed by workers (`aiohttp` sessions) which fetch HTML, parse the page, and attempt logo extraction.
The system architecture is based on an asynchronous task queue model. The task queue is populated with a task for each domain. Tasks are processed by a pool of workers coroutines (`aiohttp` sessions) which fetch HTML, parse the page, and attempt logo extraction. The number of workers assigned is $1/50$ of the number of domains to balance speed and overhead. One point of improvement may be to improve this heuristic.

Each worker fetches the HTML of a domain and attempts to extract a logo using

1. JSON-LD Extraction – Tries to find structured data in `<script type="application/ld+json">` blocks, searching for known schema patterns that commonly contain logo URLs from trial and error. The primary mechanism to distinguish between possible logos and noise is to check if the string contains the substring "logo". If no logo is found from checking the "common" structures, we fallback to a recursive function which recursive checks the if there exists a "logo" in any of the strings. This strategy is a heuristic and prone to mistakes. One point of improvement may be to refine this strategy like expanding the keywords used to accept a possible candidate for a logo url. Moreover, if there are multiple candidates, we return the candidate with the longest length (this heuristic also needs work).
2. Favicon Extraction – If no JSON-LD logo is found, attempts to find `<link rel="icon">` or similar tags that reference `.png`, `.ico`, or other favicon formats. The strategy utilised here is the same strategy as the one for JSON-LD extraction.

All URLs are standardized via `urljoin` to ensure absolute paths. Retry logic is included for resilience, with exponential backoff. Results are recorded with timing and source metadata.

The crawl can be triggered by calling the `crawl_with_queue(domains)` function to instantiate the LogoCrawler object, populate the task queue, and run the worker pools.

Unit tests for the program can be found at the last part of the file to provide more clarity as to the specification of each function.

### Key Components

The main components of the project are the following:

1. Input Parser: Reads newline-separated domain names from STDIN.
2. LogoCrawler Class: Core logic component using asynchronous workers to crawl and extract logos.
3. Worker Logic: The function that each worker executes to process a task from the task queue as they attempt to extract a logo from a domain.
4. Fetch Function: The function which hierarchically searches for 1) the logo URL using structured JSON-LD data, 2) fallback image or meta tags that likely reference a logo, and 3) favicon links if no better options are found.
5. Logging: All crawl activity, including fetch attempts, errors, and summary statistics, is logged to both the console and `py/logocrawler/logs.txt` for persistent debugging and analysis.
6. Result Aggregator: Collects statistics about the crawl job (e.g., strategy used, attempt durations, errors, etc.).
7. Output Writer: Converts results into a structured CSV format and writes to STDOUT.

---

## Measuring Success

I used the following methods to assess the quality of my logo crawler's results:

1. Logging: A logger tracks each attempt, its duration, and whether it was successful or not for each domain. It shows whether a logo was found, a favicon, or nothing at all. For a logo and favicon, it shows the url.

2. Statistics: The `main.py` script includes a summary function that processes the crawl results and logs key metrics, including the total number of domains, the number of successful and failed logo extractions, the count of logos found via JSON-LD and favicon, the number of domains with no logo found, as well as performance metrics such as total, average, and maximum roundtrip time (including the domain with the longest response time). The total time of the crawl job is also recorded separately.

3. Tests: Dozens of unit tests simulate extraction cases (nested JSON-LD, broken links, redirects).

---

## Precision and Recall

### Limitations

Precision and recall are difficult to measure quantitatively. Currently, I manually review a sample of the crawler’s results to verify whether it returns the correct logo URL and whether it successfully finds a logo when one is available. Overall, the crawler performs well, but there are cases where it either fails to detect an available logo or returns an irrelevant URL (e.g., one that is not a logo or favicon).

The most common failure cases include:

1. Redirects: Some domains redirect to a different website, and the crawler does not always follow these redirects reliably, resulting in missed logos or favicons.

2. Unconventional URLs: Some logos are located at non-standard paths or hosted under asset domains, such as `https://secure.skypeassets.com/apollo/2.1.1806/images/icons/apple-touch-icon.png`, which the current heuristics may not detect.

These areas represent valuable opportunities for further improvement, particularly in enhancing redirect handling and expanding the crawler’s ability to recognize non-standard logo URLs.

### Measuring Precision and Recall

Measuring precision and recall for a logo crawler is challenging due to the lack of a ground truth dataset, but here's a practical approach:

---

### Measuring Precision and Recall

Measuring precision and recall is difficult because it requires manually verifying whether the returned URL actually points to a relevant logo as sometimes a URL may appear correct (e.g., containing "logo") but does not lead to a meaningful or visible logo on the site.

To evaluate the crawler's performance, I would do the following if I had more resources:

1. Create a Labeled Ground Truth Sample

a. Randomly sample a subset of domains (e.g. 100–200).
b. Manually inspect each domain’s homepage and record:

- The correct logo URL (or mark if none is present).
- Whether a favicon is a valid substitute.

This becomes the ground truth dataset.

2. Run the Crawler on the Sample

For each domain in the sample, run the crawler and compare the result to the expected logo URL.

3. Define Metrics

a. True Positive (TP): Crawler returns the correct logo (matches or equivalent).
b. False Positive (FP): Crawler returns an incorrect or irrelevant URL (e.g., non-logo image).
c False Negative (FN): Crawler fails to return a logo when one exists.

Then compute:

```
Precision = TP / (TP + FP)
Recall = TP / (TP + FN)
F1 Score = 2 * (Precision * Recall) / (Precision + Recall)
```

The F1 score is the harmonic mean of precision and recall. It provides a single, balanced metric that accounts for both false positives and false negatives. This is especially useful in the logo crawling context, where there’s often an uneven distribution of outcomes (e.g., many domains without structured logos), and we care equally about not missing logos (recall) and avoiding incorrect ones (precision). F1 gives a more holistic view of overall performance than either metric alone.

4. Add Tiers

Since "correctness" can be subjective (e.g. favicon vs main logo), I would build on top of precision and recall and develop the following tiers:

1. Strict: Only exact logo matches count as true positives. This is useful when high accuracy is critical and only the main logo is acceptable.

```
Strict Precision = TP_strict / (TP_strict + FP_strict)
Strict Recall = TP_strict / (TP_strict + FN_strict)
Strict F1 = 2 * (Strict Precision * Strict Recall) / (Strict Precision + Strict Recall)
```

2. Relaxed: Also count favicons or logo-like images as valid. This tier captures more realistic, near-correct outcomes.

```
Relaxed Precision = TP_relaxed / (TP_relaxed + FP_relaxed)
Relaxed Recall = TP_relaxed / (TP_relaxed + FN_relaxed)
Relaxed F1 = 2 * (Relaxed Precision * Relaxed Recall) / (Relaxed Precision + Relaxed Recall)
```

These two tiers provide a more complete evaluation where strict metrics show exact correctness, while relaxed metrics reflect practical usefulness.

---

## Scaling to Millions of Domains

With the current setup, it takes 3 minutes to go through 1000 doimains from `websites.csv`.

`[2025-06-01 02:33:49] [INFO] [TOTAL PROGRAM TIME] 196.26 seconds`

### Current Limitations

- In-memory queue
- Single-machine I/O and CPU constraints
- Basic rate-limiting using `asyncio.sleep(self.min_delay)`

### Future Enhancements

- Use Redis/Kafka for distributed queues
- Worker pool across multiple machines (Celery or Dask)
- Domain-aware throttling and rate-limiting
- Store intermediate state and retries to disk or DB
- Use headless browsers (e.g., Playwright) for JavaScript-heavy sites
- Track per-domain error rates to prioritize retry strategies

---

## Development Environment

- Python 3.8+
- Uses only minimal dependencies: `aiohttp`, `beautifulsoup4`, and `flake8`, `black` for formatting.
- Reproducible setup using `default.nix` + `nix-shell`

---

## How to Run

```
cat websites.csv | python py/logocrawler/main.py > output.csv
```

---
