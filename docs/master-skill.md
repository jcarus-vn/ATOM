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
  concept, 3 levels. The original chapter/section structure of each
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

## Taxonomy lookup, measured

`tools/eval_taxonomy_lookup.py <master-dir>` compares looking a
concept up through the taxonomy index against a flat scan of all
notes. Measured on a 12-concept fixture master (12 questions, this
repo's test suite):

- Flat scan: 12/12 found, 6.5 notes scanned on average, 364 tokens on
  average.
- Taxonomy index: 12/12 found, 65 tokens on average (index amortized +
  one note).

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
