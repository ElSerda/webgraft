---
name: webgraft
description: "Answer web questions from opened pages, not snippets."
version: 1.3.0
author: ElSerda
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Research, Web, Search, Grounding, Sources]
    category: research
    related_skills: [grounded-citations, pdf]
---

# Web Research Skill

Answer from pages you actually read: search to find them, `web_extract` to read them.

The webgraft plugin usually appends the pages you opened under your answer; its note on the user's
message says whether it does. When it does, do not write a sources list yourself; when it does
not, end your answer with the URLs of the pages you used.

For a document or report with numbered inline citations, use `grounded-citations` instead.

## When to Use

- The user asks to search, find, check or compare something.
- The answer depends on facts that change or that you cannot be sure of: specs, prices,
  versions, news, dates.

## Prerequisites

The webgraft plugin, with `web.extract_backend: webgraft` (run `hermes webgraft setup`)
and any search backend for `web_search`.

## Quick Reference

| Step | Tool |
|---|---|
| Find candidate pages | `web_search` with a precise query |
| Read one page or PDF | `web_extract` with one URL |
| Read a long saved result | `read_file` on the path in `<persisted-output>` |

## Procedure

### Find pages

1. Call `web_search` with a precise query.
   - Keep product names and model numbers exact.
   - Use English for technical topics.

2. Pick the 2 or 3 most relevant results.
   - Prefer official sources: maker pages, datasheets, documentation.

### Read pages

3. Call `web_extract` with one URL per call, never several URLs together.
   - It reads PDFs too, so open datasheets directly.
   - If you pass several URLs, only the first is read and `webgraft_note` lists the others:
     open the ones you still need, each in its own call.

4. If a result returns `error`, open the next result.
   - One failure never ends the research.

5. A page's result ends with "Links on this page": to open a product, an article or a section it
   names, take its URL there instead of searching for it.

### Answer

6. If the pages do not answer the question, search again with different words, up to 3 searches,
   one at a time.
   - A search with no result is not proof that something does not exist: the search engines may
     be rate-limiting; search again with other words or open a page that lists it.

7. Answer using only facts found in the pages you opened.
   - Keep numbers and units as written.

## Pitfalls

- Search snippets are not sources, even when they hold numbers. Open the page.
- Never say a tool cannot reach the information before trying it.
- A fact you did not find stays missing: say so.
  - A fact from memory is marked `(from memory, not verified)`.
- Marketing pages often omit specs. Look for the datasheet or a detailed review.
- Reseller and review pages can copy the wrong values.
  - When sources disagree, give both values and prefer the maker's page.
- A `<persisted-output>` result is only a preview. Read the saved file with `read_file`.

## Verification

Before answering, check that you called `web_extract` and that every value you give appears in
the pages you opened.
