# Master skills

A master skill merges many books and web articles from ONE field into
one skill. It is the "atomic brain" layer for a field: many fields mean
many masters, one per field, each isolated from the others.

## Architecture: notes are the data, the rest are views

- **Notes are the source of truth.** Every concept is one atomic note in
  `notes/<slug-id>.md`: exactly one idea, in distilled words (never a
  long verbatim copy), with a stable slug id (no Luhmann-style
  numbering like 1, 1a, 1a1), its sources, its status, its facets, and
  its typed links to other notes. There is no fleeting-note layer:
  collecting raw notes is the user's job, not this converter's.
- **Glossary and patterns are views** rendered from the notes, grouped
  by kind. They say so in their header. Editing a note and rebuilding
  updates the views; editing a view directly will be overwritten.
- **Taxonomy is the navigation index** (`taxonomy.md`): field > area >
  concept, 3 levels, labels and note links only (citations live in the
  notes). A concept is filed under the area whose name matches its topic
  facet or source chapter; with no match it is listed as unplaced rather
  than filed under an arbitrary area. Nodes marked `"manual": true` in
  `master.json` keep the parents a person gave them on every rebuild. The original chapter/section structure of each
  source stays the ground truth; the taxonomy never rewrites content to
  fit it. A concept may sit under several areas (polyhierarchy), and a
  concept shared with another field is cross-linked in
  `cross-field-links.md`, never merged across fields.
- **Graph is the relations** (`relations.md` + machine-readable
  `graph.json`): typed edges - `prerequisite_of`, `contrasts_with`,
  `example_of`, `part_of`, `used_in` - each with its sources.
  Alongside the tree, a light relation layer records prerequisite,
  opposition, and example relations that a pure tree cannot express.
- **Facets are the filter** (topic, technique, level, source_type) on
  every note. Facets filter; they do not replace the taxonomy.
- **Registry is the management layer** (`index.json` at the masters
  root): one entry per master with field, slug, source list, concept
  count, contested count, version, and update time. It is rewritten on
  every build and fold-in.

In short: taxonomy = navigation skeleton, graph = relations, facet =
filter, registry = management, notes = data.

## Statuses and traceability

Every claim lists its sources: book + chapter (page/figure when known),
or web URL + fetch date with the note that the live page may have
changed. Statuses:

- `single-source`: one source only.
- `consensus`: several sources, same definition.
- `contested`: several sources, different definitions. Every side is
  kept with its source; the builder never silently picks a winner.

A master skill is a consensus synthesis, not a new truth. Contested
points stay visible for the reader to decide.

## Usage

Manifest format (`manifest.json`):

```json
{
  "field": "Decision Science",
  "sources": [
    {
      "name": "Book A", "kind": "book",
      "chapters": [{"title": "Judgment under uncertainty"}],
      "glossary": [{"term": "Anchoring",
                    "definition": "People rely too heavily on the first number they see.",
                    "chapter": "Judgment under uncertainty",
                    "topic": "Judgment", "level": "basic"}],
      "patterns": [{"name": "Framing",
                    "description": "Present the same choice as a gain or a loss.",
                    "chapter": "Judgment under uncertainty"}],
      "relations": [{"from": "Anchoring", "type": "prerequisite_of",
                     "to": "Loss aversion"}],
      "aliases": {"Loss aversion": ["loss-aversion"]},
      "taxonomy": {"areas": [{"name": "Judgment",
                              "topics": ["Anchoring"]}]}
    },
    {
      "name": "Decision Lab Notes", "kind": "web",
      "url": "https://example.com/anchoring",
      "fetched_at": "2026-10-27T01:00:00+00:00",
      "glossary": [{"term": "Anchoring",
                    "definition": "People rely too heavily on the first number they see."}]
    }
  ]
}
```

Commands:

```bash
python3 scripts/build_master.py --field "Decision Science" --skills-json manifest.json
python3 scripts/build_master.py --field "Decision Science" --skill-dir ~/.agents/skills/some-skill
python3 scripts/build_master.py --list-masters
python3 scripts/build_master.py --query --status contested
python3 scripts/build_master.py --query --facet level=advanced --facet topic=pricing
python3 scripts/build_master.py --query --term anchoring
```

Masters live under `<masters-root>/<field-slug>/` (default root
`~/.agents/skills/masters`, override with `--masters-root` or
`BOOK_SKILL_MASTERS_ROOT`). The registry is `<masters-root>/index.json`.

## Fold-in: adding a source later

Re-run the build command with the new source. Sources already present
(matched by name + URL) are replaced with their new data; new sources
are appended. Notes merge by normalized title plus the alias map: a
concept that already exists gains the new source and a recomputed
status instead of a duplicate note. Taxonomy updates incrementally:
existing nodes keep their labels and parents (only status and sources
refresh), new concepts are appended, the version bumps. Hand-edited
note bodies survive fold-in because existing `notes/*.md` files are
parsed back first; the machine-readable `master.json` is regenerated
from the same build so the two never drift within one build.

## Asking in another language

Notes keep the language of their sources, and the ranker matches words,
so a question in another language finds little: on the Robotics master,
22 gold questions translated to Vietnamese reach recall@5 0.44 against
0.88 in English. Give concepts their names in that language:

```bash
python3 scripts/build_master.py --field "Robotics" --export-aliases names.json
# fill each "aliases" list (an agent can translate the titles), then:
python3 scripts/build_master.py --field "Robotics" --import-aliases names.json
```

Names are stored in `<master>/aliases.json` (`notes`: note id to names,
`labels`: topic or taxonomy label to names). The builder never writes
that file, so rebuilds and fold-ins keep it; importing again adds to it.
A compact build carries the names into its registry and index. This is
separate from a manifest source's `aliases`, which only merges spellings
of one concept at build time.

How names are matched: a concept name counts like a second title, on
pairs of adjacent words (so "vi sai", differential, does not match
"sai số", error) plus ASCII words such as acronyms; a topic or area
name counts like its label. Measured with `tools/eval_multilingual.py`
on the Robotics master, 22 questions per set, recall@5:

| Questions | Without names | With names |
| --- | ---: | ---: |
| English | 0.88 | 0.88 |
| Vietnamese, standard terms | 0.44 | 0.86 |
| Vietnamese, other wording | 0.20 | 0.73 |
| Vietnamese with English terms mixed in | 0.91 | 0.89 |

The Vietnamese questions and the names were written by the same
translator (an AI), so the second row is optimistic; the "other wording"
set, written to avoid the names' terms, is closer to a real reader. A
question that describes a concept without naming it ("learning by trial
and error") still finds nothing: names fix terminology, not paraphrase.

An agent can also translate the question itself and pass both wordings:

```bash
python3 scripts/build_master.py --question "Lập kế hoạch chuyển động là gì?" \
    --also "What is motion planning?"
```

Each wording is ranked and the scores are summed per note, so a wording
that matches nothing changes little; the answer threshold still applies
to each note's best single-wording score. Same master and questions,
recall@5, with the English gold question standing in for the agent's
translation (a perfect translation, so an upper bound):

| Questions | Names | Translation | Names + translation |
| --- | ---: | ---: | ---: |
| Vietnamese, standard terms | 0.86 | 0.88 | 0.90 |
| Vietnamese, other wording | 0.73 | 0.82 | 0.85 |
| Vietnamese with English terms mixed in | 0.89 | 0.92 | 0.90 |

Summing scores beat reciprocal rank fusion here (0.56 / 0.39 / 0.91
for the translation column), because ranks give an unmatched wording's
noise the same weight as real hits.

## Semantic retrieval (experimental, off by default)

Names and translations help a question that uses the concept's words.
One that describes a concept in other words ("learning by trial and
error") needs meaning, not words: compare question and notes as
embeddings from a multilingual model. This is optional and **not yet
measured on a real model**: the development environment could not
download one. Its gate before it is recommended: Vietnamese "other
wording" recall@5 at least 0.8 on the Robotics set, with English not
below 0.88.

1. `pip install "atom-brain[semantic]"` (onnxruntime, tokenizers, numpy;
   the current onnxruntime needs Python 3.11+, older Pythons get an older
   release).
2. Put a model on disk yourself; ATOM never downloads one. The directory
   needs an ONNX export (`model.onnx`, `model_quantized.onnx`, or either
   under `onnx/`) and its `tokenizer.json`, e.g. an ONNX export of
   `intfloat/multilingual-e5-small` (0.1B parameters, 384 dimensions,
   about 470 MB, or about 120 MB quantized). Defaults follow E5's
   `query: ` / `passage: ` prefixes; a different model can set
   `query_prefix`, `passage_prefix` and `max_length` in an
   `atom-embed.json` beside it.
3. Index a master, then ask as usual:

```bash
python3 scripts/build_master.py --field "Robotics" --embed --embed-model ~/models/e5-small
export ATOM_EMBED_MODEL=~/models/e5-small
python3 scripts/build_master.py --question "Robot tự học bằng thử và sai là gì?" \
    --also "What is learning by trial and error?"
```

The index is `embeddings.json` + `embeddings.f16` in the master directory
(one float16 row per note: about 90 KB for 118 notes at 384 dimensions).
Rebuilds keep it and say it may be stale; `--embed` recomputes only the
notes whose text changed. `tools/build_compact.py` copies it, so
`--compact-dir` queries use it too. The semantic ranking is fused with
the word ranking by rank (reciprocal rank fusion), so it can only add
candidates and reorder them; a hit with no word support is shown only
when its cosine reaches `--min-cosine` (default 0.80, **uncalibrated**:
cosine scales differ between models).

Measure a model before trusting it:

```bash
python3 tools/eval_multilingual.py <master_dir> --embed-model ~/models/e5-small
```

It adds a "semantic" and an "all three" column and prints the cosine of
each question's best gold note and best other note, to set `min_cosine`
(store it in `embeddings.json` or pass `--min-cosine`). Fusion by rank
gives a weak model's ranking as much weight as the word ranking: in a
plumbing test with a toy model that knows almost no words, "all three"
fell to about 0.36 on the Vietnamese set. A model that does not beat the
"names + transl." column should not be enabled.

## Taxonomy lookup, measured

`tools/eval_taxonomy_lookup.py <master-dir>` compares looking a
concept up through the taxonomy index against a flat scan of all
notes. Measured on a 12-concept fixture master (12 questions, this
repo's test suite):

- Flat scan: 12/12 found, 6.5 notes scanned on average, 364 tokens on
  average.
- Taxonomy index: 12/12 found, 64 tokens on average (index amortized +
  one note); 158 tokens for the first lookup of a session, before the
  index is amortized.

The index cost is measured on the rendered `taxonomy.md`, the file an
agent reads. On the 118-concept Robotics master that is about 1,000
tokens (labels and note links only); the first lookup of a session costs
about 1,040 tokens, and a ranked `--question` query plus the notes it
returns about 660 tokens with nothing amortized.

The index does not find more than the flat scan; it finds the same
answers while loading less. Re-run the script on your own master before
quoting a number for it.

## Limits, stated plainly

- The taxonomy is only as stable as its generation: an LLM-generated
  taxonomy can shift between runs, so a human should review node
  placement before treating it as settled.
- Entity resolution is by normalized title plus explicit aliases. Two
  different names for the same concept (or one name for two concepts)
  still need a manual alias or a hand merge; the graph is only as good
  as that resolution.
- Notes are distilled words, so for exact wording always check the
  source the note cites; a note is a pointer to knowledge, not a
  replacement for the book.
- A master skill does not turn agreement into truth: several sources
  can share the same mistake, and a contested point with one strong
  source against many weak ones is still recorded as contested.
