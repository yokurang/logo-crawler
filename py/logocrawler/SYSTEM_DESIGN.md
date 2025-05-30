# LogoCrawler

LogoCrawler is a Python program that crawls a list of domains and extracts the best possible logo URL for each site. It uses asynchronous HTTP requests with a worker queue, retries failed requests, and logs detailed results.

## How to Run

You can use this in two ways:

1. Using a domain list file:

    python py/logocrawler/main.py websites.csv > output.csv

2. Or using standard input:

    cat websites.csv | python py/logocrawler/main.py > output.csv

The output is written to standard output as CSV with two columns: `domain,logo`.

## Nix Shell (Recommended)

This project includes a `default.nix` to provide a consistent dev environment.

To launch the shell:

    nix-shell

This installs all required packages including `aiohttp`, `beautifulsoup4`, `pytest`, etc.

## How It Meets the Requirements

### Input and Output

- Reads domain names from standard input.
- Writes CSV to standard output (`domain,logo`).
- Sample input: `websites.csv` provided.

### Scraping Strategy (Ordered)

1. JSON-LD metadata in `<script type="application/ld+json">`
   - Looks for nested `"logo"` or `"url"` fields.
2. `<link rel="icon">` elements
   - Prefers `.png`, then `.ico`, then longest URL.
3. Fallback: `https://<domain>/favicon.ico`

This prioritizes semantic correctness and structured data over weak heuristics.

### Async Queue System Design

- Domains are queued and processed concurrently using `asyncio` and `aiohttp`.
- Concurrency is managed via `asyncio.Semaphore`.
- Each domain is attempted up to 3 times with exponential backoff.
- A single `LogoCrawler` manages the worker queue and result collection.
- Logs progress, success/failure, and time spent per domain.

### Attempt Logging and Statistics

Each domain stores:

    attempts = [(duration_in_seconds, failure_reason_or_None), ...]

This allows precise analysis of retries, bottlenecks, and error causes. After completion, a summary is printed including:

- Total domains, successes, failures.
- Source types: JSON-LD, favicon, fallback.
- Total and average roundtrip times.

### Design for Precision and Recall

- Favors structured metadata to improve precision.
- Falls back only if no logo-like link is found.
- Favicon is not prioritized unless explicitly missing other options.
- All extracted URLs are resolved to absolute `https://` links.

### Scaling Considerations

To scale this system to millions of domains:

- Use persistent distributed queues (Redis, Kafka).
- Persist progress and errors to disk or database.
- Use domain-aware throttling and rate-limiting.
- Parallelize with distributed workers or async task pools.
- Incorporate headless browser rendering for JavaScript-heavy sites.
- Monitor structured error rates to tune retry strategies.

## Testing

To run unit tests:

    pytest py/logocrawler/test_crawler.py

## Dependencies

- Python 3.8+
- aiohttp
- beautifulsoup4
- pytest

All dependencies are listed in `default.nix` for reproducibility.

## Notes

This implementation avoids unnecessary third-party packages and focuses on readable, testable logic that meets the project’s core goals. Additional enhancements (e.g., screenshot-based detection, HTML layout parsing) are deferred for discussion.

