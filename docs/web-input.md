# Web input

A URL is a source like a book: it goes through the same extraction
pipeline and lands in the same `full_text.txt` and `metadata.json`.

## Usage

```bash
python3 scripts/extract.py https://example.com/article
python3 scripts/extract.py https://a.com/1 https://b.com/2
python3 scripts/extract.py links.urls   # one URL per line, '#' comments ignored
```

A failed fetch is a skipped source with a warning, never a failed run.

## What is recorded

Every web source in `metadata.json` carries:

- `url`: the original URL
- `title`: the page title
- `fetched_at`: the fetch time (UTC ISO)

Cite a web source as URL + fetch date, and note that the live page may
have changed since the fetch. The same rule applies inside skill and
master-skill files: a web citation without its fetch date is incomplete.

## Boilerplate stripping

Main content is separated from navigation, advertising, footers, and
other chrome: trafilatura when installed, otherwise a stdlib pass that
drops `nav`/`aside`/`footer` subtrees and ad-like elements, falling back
to the repo's HTML extractor when the strict pass yields too little.
Pages that render their content with JavaScript will usually yield
little or nothing; this tool does not run a browser.

## Rights boundary

This tool only fetches publicly readable pages the user has the right
to read. It sends no credentials, does not log in, and does not bypass
paywalls. Do not point it at content the user is not entitled to read,
and do not treat a fetched page as permission to redistribute it: the
generated skill synthesizes, it does not republish.
