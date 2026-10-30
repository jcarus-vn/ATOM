#!/usr/bin/env bash
# Assemble the MkDocs sources from README.md / SKILL.md / BACKERS.md.
# Shared by the build (PR) and deploy (master) jobs of deploy-docs.yml.
set -euo pipefail

# README.md carries no frontmatter (it has to stay clean on GitHub), so
# the Guide page would otherwise inherit the generic site description.
{
  printf -- '---\n'
  printf -- 'description: "Convert a book, PDF, EPUB or DOCX into an agent skill for Claude Code, GitHub Copilot CLI and Amp. Install, run, and cut token cost 24x-51x."\n'
  printf -- 'seo_title: "ATOM Guide - Turn Books and Docs Into an Atomic Brain"\n'
  printf -- '---\n\n'
  # `width="100%"` is what GitHub wants, but it is not a valid HTML
  # width, so browsers discard it and reserve no space for the banner —
  # everything below it jumps once the image loads. Real dimensions let
  # the browser hold the slot; Material's `height:auto` keeps it fluid.
  sed -E 's,(href="|src="|\]\()docs/,\1,g' README.md \
    | sed -E '/banner\.webp/s,width="100%",width="1600" height="686",'
} > docs/guide.md
# SKILL.md's `description` is written for host agents (~400 chars); as a
# meta description it just gets truncated mid-sentence in the SERP.
sed '0,/^description:/s|^description:.*|description: "The full ATOM converter spec: every step, depth budget, extraction mode, and quality rule the agent follows to turn a document into a skill."\nseo_title: "Skill Reference - The Complete ATOM Converter Spec"|' \
  SKILL.md > docs/skill-reference.md
cp BACKERS.md docs/BACKERS.md
# Mirror image: the docs pages link back with `../README.md`, correct on
# GitHub, a 404 on the site — where that same file is published as guide/.
sed -i -E 's,\]\(\.\./README\.md\),](guide.md),g' docs/*.md
