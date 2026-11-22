---
name: webgraft
description: "Answer web questions from opened pages, not snippets."
version: 1.6.0
author: ElSerda
license: MIT
platforms: [linux, windows]
metadata:
  hermes:
    tags: [Research, Web, Search, Grounding, Sources]
    category: research
    related_skills: [grounded-citations, pdf]
---

# Web Research Skill

Answer from pages you opened: `web_search` finds them, `web_extract` reads them (PDFs too).
Do not cite or name your sources yourself, not even in one sentence: webgraft shows the pages you
opened under your answer (a URL the user asks for is fine). Only if webgraft's note on the user's
message asks for them, end your answer with the URLs you used. For numbered inline citations, use
`grounded-citations` instead.

## When to Use

Searching, checking or comparing facts that change or that you are unsure of: specs, prices,
versions, news, dates.

## Prerequisites

The webgraft plugin as `web.extract_backend` (`hermes webgraft setup`).

## Quick Reference

`web_search` to find pages, `web_extract` with one URL per call to read them, `read_file` for a
`<persisted-output>` preview.

## Procedure

1. Search for exactly what the user asked. Keep names verbatim, never translate them
   ("Nous Research" stays `"Nous Research"`). Quotes, English for technical topics and `site:`
   change the syntax, never the names.
2. Open the 2 or 3 most relevant results, official sources first (maker pages, datasheets,
   docs, releases). For "latest / newest": compare publication dates with the current date in
   your context and open the official releases page; never present an older item as current.
3. `web_extract` one URL per call. If its `webgraft_note` lists pages it did not open, open the
   ones you still need, each in its own call. To follow a page's "Links on this page", take the
   URL there instead of searching.
4. A result with `error`: open the next result. No answer in the pages: refine the query and
   search again, at most 3 searches, then report what you found and what is missing.
5. Answer only with facts from the pages you opened, numbers and units as written.

## Pitfalls

- Search snippets are not sources, even when they hold numbers.
- An empty search is not proof something does not exist: the engines may be rate-limiting.
- When sources disagree, give both values and prefer the maker's page.
- A fact you did not find stays missing; one from memory is marked `(from memory, not verified)`.

## Verification

Every value you give appears in a page you opened with `web_extract`.
