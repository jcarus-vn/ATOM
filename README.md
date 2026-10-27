<p align="center">
  <img src="docs/assets/banner.webp" alt="ATOM banner" width="100%">
</p>

<h1 align="center">ATOM</h1>


<p align="center">
<strong>Stop paying your agent to re-read the same book every turn. Turn any technical book, paper, or web doc into a living atomic brain with ATOM - one master skill per field - that answers from sourced notes instead of guessing, at a fraction of the token cost, while you work in GitHub Copilot CLI, Amp, Claude Code, Codex, Hermes Agent, OpenCode, or OpenClaw.</strong></p>

<p align="center">
  <a href="https://github.com/jcarus-vn/ATOM/releases"><img src="https://img.shields.io/github/v/release/jcarus-vn/ATOM?style=for-the-badge&color=blueviolet" alt="Latest release"></a>
  <img src="https://img.shields.io/badge/Agent_Skills-Open_Standard-blueviolet?style=for-the-badge" alt="Agent Skills standard">
  <img src="https://img.shields.io/badge/PDF%20%E2%80%A2%20EPUB%20%E2%80%A2%20DOCX%20%E2%80%A2%20MD%20%E2%80%A2%20HTML%20%E2%80%A2%20RTF%20%E2%80%A2%20MOBI-supported-green?style=for-the-badge" alt="Formats supported">
  <img src="https://img.shields.io/badge/License-MIT-blue?style=for-the-badge" alt="MIT License">
</p>


> **ATOM - Atomic Thought Organizing Machine.** It converts books and
> documents and adds the master-skill system: flexible chapter detection,
> OCR for scanned PDFs and figures, web URL inputs, and field master skills
> built from atomic notes, taxonomy, typed graph, facets, and a registry
> (docs/master-skill.md). Built for projects, not casual readers: it is
> the knowledge layer other agents and assistants plug into - feed it
> documents, query it on demand.



<p align="center">
  <a href="#-why">Why</a> ·
  <a href="#-what-it-generates">What it generates</a> ·
  <a href="#-beyond-books">Beyond books</a> ·
  <a href="docs/how-it-works.md">How it works</a> ·
  <a href="docs/usage.md">Usage</a> ·
  <a href="docs/install.md">Install</a> ·
  <a href="docs/faq.md">FAQ</a> ·
  <a href="docs/performance.md">Performance</a> ·
  <a href="docs/architecture.md">Architecture</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <strong>71 tokens to find an answer, not 2,201 - on every question, every turn.</strong> Measured on a real 118-concept master skill: taxonomy lookup vs flat scan, 31x cheaper - with answer quality identical before and after compression. (<a href="docs/performance.md#the-discovery-loop-tax">how it's measured</a>)
</p>

**How it works, in 3 steps:**

1. **Point** it at a file, folder, or glob - `/atom ./my-book.pdf`
2. **It distills** the book into atomic notes and builds a skill - frameworks, decision rules, anti-patterns, and per-chapter files. Structure, not a summary.
3. **Your agent loads it on demand** - ask `/my-book replication` and it reads the right chapter and answers from the real content, no hallucination.

---

## 💰 Token math: what it actually saves you

Token cost is the whole business case. The structuring work is paid **once**, at conversion. Every query after that costs only the answer - not the book. Measured on a real 118-concept master skill:

| Every time you... | Without ATOM | With ATOM |
|-------------------|--------------|-----------|
| Route to the right note | 2,201 tokens scanned flat | **71 tokens** via the taxonomy index (31x less) |
| Answer a question in a session, all-in | Re-read the whole book text: about 266,000 tokens for this corpus, re-billed every turn | **about 1,506 tokens** amortized (index load + top-5 note bodies, measured over 22 questions) |
| Store the master | 88,901 tokens / 702 KB | **34,462 tokens / 171 KB** in compact format (61% fewer tokens, 76% fewer bytes) |
| Rank candidate notes | 14.4 ms | **1.0 ms** |

Now multiply by questions, by turns, by users. A project that asks a thousand questions does not pay a thousand book-readings - it pays a thousand lookups. That gap is the product.

One honest boundary: below roughly 5,000 tokens (about 10 A4 pages) dumping the document is still cheaper. ATOM is built for everything longer than that - and the longer the document, the bigger the win.

---

## 🤔 Why

<img align="right" width="200" src="docs/assets/booklin.png" alt="ATOM illustration">

Your agent does not have a reading problem. It has a memory problem.

Every turn it re-reads the PDF, re-finds the chapter, re-pays the token bill - and still guesses when the answer is not in front of it. The usual workarounds don't help:
- 📄 "Let me just search the PDF" → you get a list of pages, not answers
- 🧠 "I'll ask the agent about this book" → it answers from vibes when the content is not loaded
- 📝 "I'll take notes as I read" → you end up with a 200-line doc nobody ever opens again

**ATOM turns the document into a structured skill your agent loads on demand - and into a master skill when a whole field is at stake.**

Once installed, you just type `/your-book-slug replication` and the agent reads the right note and answers from sourced content. No digging through PDFs. The book becomes part of your workflow - and part of your project's brain.

Works with any host that supports the open [Agent Skills](https://github.com/agentskills/agentskills) standard - GitHub Copilot CLI, Amp, Claude Code, Codex, Hermes Agent, OpenCode, and OpenClaw all read the same `SKILL.md` format.
---

## 📦 What it generates

Running `/atom your-book.pdf` (or a folder, glob, or list of files) creates a full skill in the user-level cross-agent skills directory `~/.agents/skills/<slug>/` - one copy that Copilot CLI, Amp, Codex, OpenCode, and OpenClaw discover natively (OpenCode also reads its own managed root `~/.config/opencode/skills/<slug>/`; OpenClaw also discovers it when using its default state directory, with a non-default `OPENCLAW_STATE_DIR` use the active state's `skills/` root instead). When run under Claude Code, the converter also attempts a verified symlink at `~/.claude/skills/<slug>/` - Claude Code sees the skill when the link read-back confirms it; otherwise the run report says so. Hermes Agent partitions its personal skills by category and does not scan the cross-agent root, so a Hermes install lands in `/skills/<category>/<slug>/` instead. OpenClaw-managed, OpenCode-managed, and project-local destinations remain available when explicitly requested:

| File | Purpose | Size |
|------|---------|------|
| `SKILL.md` | Core mental models + chapter index | ~4,000 tokens |
| `chapters/ch01-*.md` … | One file per chapter, loaded on-demand | ~1,000 tokens each |
| `glossary.md` | Every key term, alphabetically sorted with chapter refs | ~1,500 tokens |
| `patterns.md` | All techniques, algorithms, and design patterns | ~2,000 tokens |
| `cheatsheet.md` | Decision tables and quick-reference rules | ~1,000 tokens |

**Chapter files are loaded on-demand** - they don't count against the skill budget until you ask about that topic.

---

## 🚀 Where it earns its keep

ATOM is infrastructure for projects that need agents to actually know things. The input is any structured prose you own and re-read constantly:

- **Virtual-assistant knowledge layer** - regulations, procedures, and domain docs become master skills the assistant queries. Bound views answer from one exact document, and conflicting rules stay visible as contested instead of being silently picked.
- **Team knowledge** - architecture decision records, runbooks, onboarding guides. Fold a whole `docs/` folder into one skill and ask it while you code.
- **Research clusters** - a stack of papers plus your own notes, merged into a single unified skill and updated as new material lands (see [Update / fold-in](#-usage)).
- **Specs & standards** - RFCs, API contracts, compliance docs you reference but never memorize.
- **Scanned archives** - photographed and scanned PDFs go through real OCR (Vietnamese + English measured at 0.3% character error) instead of producing empty skills.

If your project re-opens a document often enough to wish the agent had memorized it, that document belongs in ATOM.

---


## 🧾 Measured, not promised

A PDF-reading agent doesn't just read - it *navigates*: it re-fetches the ToC, backtracks, and re-processes all of it on every turn. ATOM pays that structuring cost **once**, at conversion, so queries stay proportional to the answer. Every number below was measured on this repo, not borrowed:

- **Lookup: 71 tokens vs 2,201** - taxonomy index vs flat scan on a real 118-concept master skill (31x cheaper).
- **Storage: 61% fewer tokens, 76% fewer bytes** with the compact format - and answer quality bit-identical before and after.
- **Speed: ranking 14.4 ms down to 1.0 ms** per question on the same master.
- **Quality, checked both ways:** a dev set and a locked holdout set (12 + 10 questions) score the same through the compact path as through the legacy path.
- **The honest floor:** below roughly 5,000 tokens (about 10 A4 pages) dumping the document is still cheaper. ATOM earns its keep on documents longer than that - and the longer the document, the bigger the win.

📊 **Full methodology, numbers, and per-book tables → [docs/performance.md](docs/performance.md#the-discovery-loop-tax)**

---

## ⚙️ How it works

Two halves: a deterministic Python **extractor** (document → clean text + metadata) and a spec-driven **generator** (your agent follows `SKILL.md` to turn that into a structured skill). On-demand chapter files keep the loaded skill small.

🔧 **Full walkthrough (Steps 0–10, extraction modes, token budgets) → [docs/how-it-works.md](docs/how-it-works.md)**

---

## 🧠 Inside ATOM

ATOM keeps the converter, then builds a brain on top of it:

```
sources (books, papers, web docs)
  -> atomic notes (one concept per note, sourced, consensus / contested / single-source)
  -> taxonomy index + typed graph + facets + registry
  -> one master skill per field, queryable on demand
```

**Tech stack:** Python 3.9+ extractor (pypdf, pdfminer, Docling for
technical books, ebooklib, python-docx, trafilatura for web), OCR via
ocrmypdf/tesseract with an opt-in vision hook for figures, and a
spec-driven generator (`SKILL.md`) that distills structure, not summaries.
Extraction runs locally; your files are never uploaded by the tool.

**Why it matters:** a flat pile of notes costs tokens on every question.
ATOM's taxonomy index routes straight to the right note: 65 tokens vs 364
scanning flat on a measured 12-concept fixture (docs/master-skill.md).
Cross-checked concepts merge into one note; disagreements stay visible as
contested, with both sides and sources, instead of being silently picked
for you.

## 🚀 Usage

`/atom <path|folder|glob> [skill-name]` - plus analyze-only, generate-from-analysis, and update/fold-in modes. After a conversion, the converter can publish the skill to GitHub (private by default) so any host installs it with `npx skills add`.

▶️ **All modes and examples → [docs/usage.md](docs/usage.md)**

💬 **In practice → [master skills](docs/master-skill.md)** - one field, one master skill: a 12-concept taxonomy lookup costs 65 tokens through the index vs 364 scanning notes flat (measured fixture).

---

## 📥 Install

```bash
# One command, any host - via the cross-agent skills CLI:
npx skills add jcarus-vn/ATOM

# Or manually - clone into your skills folder (registers /atom):
git clone https://github.com/jcarus-vn/ATOM.git ~/.claude/skills/atom
# (Copilot CLI: ~/.copilot/skills/ · Amp/cross-agent: ~/.agents/skills/)
# (Hermes Agent: ${HERMES_HOME:-$HOME/.hermes}/skills/<category>/)
# (OpenClaw: ${OPENCLAW_STATE_DIR:-$HOME/.openclaw}/skills/; ~/.agents/skills/ only with default state)
# (OpenCode: ~/.agents/skills/ - it also reads ~/.config/opencode/skills/)
```

📥 **All hosts, optional extractors, and the standalone CLI → [docs/install.md](docs/install.md)**

---

## ❓ FAQ

Common questions - "why not just dump the PDF?", cost, privacy, non-book inputs, multi-file books.

❓ **Answers → [docs/faq.md](docs/faq.md)**

---

<details>
<summary>🔧 <strong>Requirements</strong></summary>


The extractor tries tools in order per format and uses the first available. If nothing is installed, it tells you which command to run. Plain text, Markdown, reStructuredText and AsciiDoc need no extra deps.

> **Check your setup in one command:** `python3 scripts/extract.py --check` prints which extractors are installed for every format and the exact command to install anything missing - no file needed.

**PDF - choose by book type:**

| Book type | Tool | Install | Speed |
|-----------|------|---------|-------|
| Text-heavy (prose, few tables) | `pdftotext` (poppler) | `sudo apt install poppler-utils` | ⚡ instant |
| Text-heavy fallback | `pypdf` | `pip3 install pypdf` | ⚡ instant |
| Text-heavy fallback | `pdfminer.six` | `pip3 install pdfminer.six` | ⚡ instant |
| **Technical (code, tables, formulas)** | **`docling`** | `pip3 install docling` | ~1.5s/page |

> Before extraction begins, the skill asks you whether the book is **technical** or **text-heavy** and picks the right tool automatically. Docling preserves markdown tables and code blocks; pdftotext is faster for prose-only books.

> **Scanned PDFs need OCR first.** A PDF that is page images with no text layer - a photographed or scanned book - has nothing for these tools to extract. The extractor checks the first pages and stops immediately with an explanation, rather than working through the whole book to produce an empty skill. Run OCR yourself, then convert the result:
>
> ```bash
> ocrmypdf input.pdf output.pdf
> ```

**EPUB:**

| Tool | Install | Quality |
|------|---------|---------|
| `ebooklib` + `beautifulsoup4` | `pip3 install ebooklib beautifulsoup4` | ⭐⭐⭐ Best |
| stdlib `zipfile` | built-in - no install needed | ⭐⭐ Always available |

**Other formats:**

| Format | Tool | Install |
|--------|------|---------|
| DOCX | `python-docx` (fallback: stdlib ZIP/XML) | `pip3 install python-docx` |
| HTML | `beautifulsoup4` (fallback: stdlib `html.parser`) | `pip3 install beautifulsoup4` |
| RTF | `striprtf` (fallback: regex) | `pip3 install striprtf` |
| MOBI / AZW / AZW3 | Calibre `ebook-convert` (external app, not pip) | https://calibre-ebook.com/download |
| TXT / Markdown / reStructuredText / AsciiDoc | built-in | - |

---


</details>

<details>
<summary>📁 <strong>Repository structure</strong></summary>


```
ATOM/
├── SKILL.md              # Skill definition + step-by-step instructions (the generator spec)
├── scripts/
│   ├── extract.py        # Thin entrypoint wrapper (books, docs, web URLs, scanned PDFs)
│   └── build_master.py   # Build a field master skill from skills + notes
├── atom/        # Deterministic extraction package
│   ├── chapter_detection.py  # Flexible chapter splitting
│   ├── master.py             # Atomic notes, taxonomy, graph, facets, registry
│   ├── ocr.py                # Scanned-PDF OCR + figure blocks, opt-in vision hook
│   ├── web_input.py          # Web URL inputs
│   ├── parsers/              # Format parsers (pdf, epub, docx, html, rtf, calibre, text)
│   └── ...                   # config, dependencies, sanitize, utils, cli
├── tools/
│   ├── eval_taxonomy_lookup.py  # Measures taxonomy-index vs flat-scan token cost
│   └── validate_skill.py        # Checks a generated SKILL.md against host rules
├── tests/                # pytest suite (extraction, detection, master skills)
├── docs/                 # Guide, architecture, performance, master-skill docs
├── CHANGELOG.md          # release history (semver)
├── CONTRIBUTING.md       # dev setup, PR conventions, release process
├── SECURITY.md           # vulnerability reporting
└── README.md             # This file
```

---


</details>

---
## ⚖️ Copyright & fair use

ATOM ships **no book content** - not a single page. It's a converter you point at files you already own.

- **Processing is local.** Extraction and analysis run on your machine. Your files are never uploaded by this tool. (If your agent's model runs in the cloud, the text you feed it follows that provider's normal data terms - same as any prompt.)
- **You use your own copy.** Bring a book you bought, docs your company owns, or papers you have the right to read.
- **The output is your notes.** A generated skill is a structured, synthesized derivative - framework names, definitions, takeaways - not a reproduction of the text. The skill explicitly never copies raw passages (see Quality Rule #7). Treat it like handwritten study notes: yours, for personal use.
- **Don't redistribute.** Publishing or sharing a generated skill of a copyrighted work can infringe the rights holder. Keep skills of third-party books private. Internal docs, your own writing, and openly-licensed material are fine to share within the bounds of their license.

When in doubt, follow the license or terms of the source document. This project is a tool; how you use it is on you.

---

## License

MIT - applies to the converter (code + skill definition) in this repository, **not** to any book or document you process with it.


