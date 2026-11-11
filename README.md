# webgraft

A Hermes Agent plugin for web answers built from pages that were really read.

- **Reads pages and documents on your machine** for `web_extract` (HTML, PDF, Word, PowerPoint, Excel, OpenDocument, EPUB, RTF, CSV): no API key, no extraction service.
- **Reports HTTP errors and bot walls as errors**, so the model never takes them for content.
- **Lists the pages the answer really used**, under the answer, with clickable links. Pages are taken from the tool results, never from the model's text, so the list cannot name a page that was not read.
- **Pushes the model to open pages** instead of answering from search snippets: a bundled skill and a short reminder.
- **Keeps long pages useful to small models**: one URL per `web_extract` call, and on a page longer than what Hermes shows the model, the passages about the question come first. See [What the model sees of a long page](#what-the-model-sees-of-a-long-page).

A real run, fully local: Gemma 4 E4B in LM Studio, DuckDuckGo search, pages read by webgraft.

```
> On each Nous Portal plan, how much RAM and disk does the included Hermes Cloud agent get,
  and what does each plan cost?

*   **Free:** $0, pay as you go (No included cloud agent)
*   **Plus:** $20 / month (4 GB RAM, 25 GB disk)
*   **Super:** $100 / month (4 GB RAM, 100 GB disk)
*   **Ultra:** $200 / month (8 GB RAM, 100 GB disk)

Sources (opened pages):
- Hermes Cloud — Always-On AI Agent Hosting: https://portal.nousresearch.com/cloud
- Nous Portal Subscription: Plans, Credits, and Hermes Agent Setup — Agentic Bo...: https://agenticbotsitter.com/guides/nous-portal-subscription-hermes-agent/
```

Links are written out in full rather than as Markdown links: the Hermes CLI prints replies as plain text, where a bare link stays clickable (Ctrl+click in Windows Terminal) and Desktop and chat apps link it too.

The footer is in English or French. By default it follows Hermes's `display.language`, like Hermes's own notices; set `language: answer` to follow the language of the reply instead.

## Contents

- [Why](#why)
- [How a page is read](#how-a-page-is-read)
- [What the model sees of a long page](#what-the-model-sees-of-a-long-page)
- [Which pages are listed](#which-pages-are-listed)
- [Install](#install)
- [Settings](#settings)
- [What it does, for review](#what-it-does-for-review)
- [Other plugins that rewrite replies](#other-plugins-that-rewrite-replies)
- [Limits](#limits)
- [Dependencies](#dependencies)
- [Development](#development)
- [License](#license)

## Why

A web answer can go wrong in three places, and the model cannot see any of them.

1. **The page reader drops the data.** Main-text extractors discard what looks like page furniture.
   - On the Nous Portal home page, trafilatura keeps the text but drops the plan list with each plan's Cloud agent size ("up to 4 GB RAM and 25 GB disk", "up to 8 GB RAM and 100 GB disk"). Without it, the answer above could not be given from that page.

2. **The page is not a page.** A 404 or a bot wall ("Automated bot check in progress") read as content gives the model nothing, or worse, a page of the wrong text.
   - Bot walls often answer HTTP 200.

3. **The model answers from snippets or memory.** Small models in particular stop at the search results when a snippet holds a number, or fill gaps from memory.
   - In tests, a small model gave a version number as the latest release with the releases page open, where no such version appears.

webgraft fixes the first two, pushes against the third, and shows which pages the answer came from so you can check it yourself.

## How a page is read

1. **What the site says about language models.** If the site publishes an [`llms.txt`](https://llmstxt.org) that lists a Markdown (`.md`) version of the page, that version is read instead of the HTML: cleaner text, and what the site asked for.
   - The file is fetched once per hour per site and checked: plain text that opens with a `# ` title, never HTML (many sites answer any address with their home page).
   - When the site has an `llms.txt` but no `.md` version of the page, a line at the end of the page points the model to it.
   - A page that asks not to be used by AI comes back as an error, not as content: `noai` in the `X-Robots-Tag` header or in a robots meta tag, or the TDM Reservation Protocol (`tdm-reservation: 1`, header or meta tag).
   - `robots.txt` is not read: it addresses crawlers, and webgraft reads only the pages the user's agent asks for.

2. **Download it.** An HTTP error (404, 410, 5xx) is reported as an error, unless a browser can still open the page.

3. **Documents** (PDF, Office, OpenDocument, EPUB, RTF, CSV): Markdown, tables included, with anydoc, the converter Hermes's own `read_file` uses (its `doc-extract` extra, installed by `setup` or on first use).
   - It runs in a separate process stopped after 60 seconds (and, on Linux and macOS, limited to 2 GB of address space and run with 4 threads), so a crafted document cannot hang Hermes or take over the machine.
   - A scanned PDF (page images, no text layer) is read by local OCR when it is installed: RapidOCR, no language model, about 1 second per page; the text comes with a note that it was read by OCR. Without OCR, the error names the scanned pages.

   HTML: main text with trafilatura.

4. **With Crawl4AI installed**, the same page is also opened in a headless browser (the Chromium Hermes installs).
   - Lines of the browser view that hold values missing from the main text, such as spec table rows, are appended under "More values from this page".
   - Pages that trafilatura cannot read, because scripts build them (Vue, React...), use the browser view instead: the browser waits for the page to load, then one more second for its scripts to fetch their data.

5. **Errors come from the HTTP status only**: a 404, 410, 403 or 5xx is reported as such.
   - A page that answers 200 is never called an error because of its text, since a table, a price or a blog post can hold "404" or "not found".
   - Bot walls can answer 200, so a 200 page is checked for one: the response headers (Cloudflare marks its challenges with `cf-mitigated: challenge`), then the text, where a challenge page's own phrases ("Just a moment...", "Checking your browser", "Automated bot check") count on a page of any length, and words an article also uses ("captcha", "access denied") only on a short page that does not declare itself an article in its HTML head.
   - Errors and walls are reported in Hermes's `HTTP <code> from origin: ...` form. Hermes then does not re-fetch them through its keyless rescue (an outside service that can return the same wall as content), and a wall that another backend returns is never listed as a source.

6. **The page's links whose text is in the content are listed last**, under "Links on this page" (40 at most): the products of a listing, the articles of an index, the sections of a documentation.
   - Extraction keeps a link's text and drops its target, so without this a model that reads a category page sees names it cannot open and searches for their addresses instead.
   - Navigation and footer links, which the extraction already dropped, stay out.

Each page is capped at 30,000 characters, links included.

## What the model sees of a long page

This is a limit of Hermes's `web_extract` that every extraction backend runs into. Small local models and models with a short context feel it most.

### The window

Hermes shows the model at most `web.extract_char_limit` characters of each page: 15,000 by default, whichever backend read it. On a longer page the model gets the first 75% and the last 25% of that window.

The middle is saved to a file under `HERMES_HOME/cache/web`, with a note that `read_file` can page through it. Small models rarely do. So on a long document the answer is often in the part the model never sees.

A real case, on Hermes 0.22.7 with Ministral 3 3B in LM Studio. The question, asked in French: "According to https://hermes-agent.nousresearch.com/docs/user-guide/configuration, what does Hermes's `web.keyless_rescue` setting do, and what is its default?"

That page is 255,000 characters long; the setting is explained 215,000 characters in. With `focus` turned off, the model saw the start and the end of the page and answered (translated):

```
The setting web.keyless_rescue is not explicitly mentioned in the Hermes Agent documentation [...]
Default value not found explicitly.
```

With `focus` on (the default), same model, same page, same question:

```
web.keyless_rescue active when true: on a failed search or extract call, the single request retries
using the keyless free-tier backend (round-robin fallback across Exa/Parallel/Firecrawl/Keenable).
Default value: true.
```

That is what the documentation says.

### Several URLs in one call

Each page gets its own window, but they all land in one tool result. Above a share of the context window (15%, about 19,700 characters with a 32K context), Hermes replaces a whole tool result with a 1,500-character preview, and the model is left with almost nothing.

### What webgraft does about it

- **One URL per call.** When the model asks `web_extract` for several URLs, webgraft reads only the first one, the model's first choice (Hermes's `pre_tool_call` hook rewrites the call).
  - The result then lists the URLs left out (`webgraft_note`, through the `transform_tool_result` hook), and the model opens the ones it still needs in their own calls.
  - Sending the whole call back with an error was tried first: it cost a round trip, and a small model (Ministral 3 3B) then twice opened its second choice and never the page it had ranked first.
  - Setting: `one_url_per_call`.

- **An empty search is not an absence.** A `web_search` that returns no result gets a `webgraft_note`: a self-hosted backend (SearXNG) answers nothing when the engines behind it rate-limit it, which several searches sent at once trigger, and Hermes reports that as a successful search with zero results.
  - The note says to search again one query at a time or to open a page that lists the thing, rather than conclude that it does not exist.

- **Relevant passages first.** webgraft knows the question: the user's message and the model's search queries. On a page longer than the window, it moves the passages that mention the question's terms to the top.
  - Rare terms weigh more than terms found all over the page, tables of contents are skipped, and a table split by a page break stays whole.
  - A note tells the model this was done, and the rest of the page follows in its original order. Nothing is removed.
  - Pages are cut into passages of about 600 characters where they have no paragraphs, as the text of an HTML page often does.
  - In the case above, the `keyless_rescue` passage moves from 215,000 characters in to the top of the window.
  - Setting: `focus`.

- Pages are read up to 400,000 characters and handed to Hermes cut at 30,000 characters, after this reordering.

### What you can do

With a model that has room, raise the window, for example `hermes config set web.extract_char_limit 30000` (two pages then take about 16,000 tokens). Keep the default with a small context.

### For Hermes

The window is a fixed head and tail. Picking it from the question, as webgraft does, would help every backend. The query is not passed to extract providers today (`extract(urls, **kwargs)`), which is why webgraft reads it from hooks.

The 20-minute extract cache is documented for users ([Result caching](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search#result-caching)) but not in the web provider plugin guide: a provider is not told that a cache hit can skip it, and the `cached` flag Hermes sets on a hit is dropped before the result reaches plugin hooks. Keeping that flag in the result would let a plugin say, for example, that a listed source was read earlier rather than just now.

## Which pages are listed

Every page `web_extract` returned with real content in this turn.

- A page opened in an earlier turn of the conversation is added under "Also used, opened earlier in this conversation" when the answer contains one of its values with a unit, a price or a version: an answer may rely on a page read a few turns before.
- When no page was opened, the footer says that the answer relies on search snippets only.
- When the turn searched and the answer holds a value (with a unit, a price or a version) that appears in the search snippets but on no page read in the conversation, a last line names it: "From search snippets, not from an opened page: 2617 MHz".
  - That is a presence check on the number and its unit, not a judgement of the value.

## Install

Requires Hermes Agent 0.22.6 or later.

```
hermes plugins install ElSerda/webgraft
hermes webgraft setup
```

Hermes asks before installing the plugin's one Python dependency (trafilatura).

After a later `hermes plugins update webgraft`, run `hermes gateway restart` if a gateway is running: on Hermes 0.23 chat sessions run the plugin code the gateway loaded, and an update does not reload it.

Then `setup`:

1. chooses a search backend: your **SearXNG** instance (it checks that JSON output is enabled) or **DuckDuckGo** (`ddgs`, built into Hermes, no key);
2. sets `web.extract_backend: webgraft`, and installs Hermes's document converter (`doc-extract`) if missing;
3. offers **Crawl4AI**, which is optional;
4. offers **local OCR** for scanned PDFs (RapidOCR), which is optional;
5. offers to auto-load the bundled skill `webgraft:webgraft` (it tells the model to open pages instead of answering from snippets, one URL per call; about 600 tokens per session);
6. tests one real page and one missing page.

Non-interactive: `hermes webgraft setup --yes --search ddgs --crawl4ai --ocr --skill`.

Check at any time with `hermes webgraft status`.

### Uninstall

```
hermes webgraft uninstall
hermes plugins remove webgraft
```

`uninstall` puts back the web backends `setup` replaced (it records them in `HERMES_HOME/plugin-data/webgraft`), removes the skill from `skills.auto_load`, and deletes the Crawl4AI and OCR environments. Hermes's Chromium stays: it belongs to Hermes.

`hermes plugins remove` alone would leave `web.extract_backend` pointing at a backend that no longer exists.

### Crawl4AI

Crawl4AI pulls about 35 packages, so it is not installed into Hermes's environment: `setup` creates a separate one with `hermes pm` (`HERMES_HOME/environments/webgraft-crawl4ai`).

webgraft downloads no browser. Crawl4AI drives the Chromium that Hermes's own browser tool uses, found the way Hermes finds it:

- `browser.executable_path` in `config.yaml`,
- then the Chromium installed by `hermes pm install chromium` (pinned and checked by Hermes),
- then `/etc/hermes/chromium-executable-path` (Docker image),
- then a `chromium` on PATH.

If there is none, `setup` offers `hermes pm install chromium`.

For each page, a fresh headless instance starts with a temporary profile and a DevTools port on 127.0.0.1, Crawl4AI connects to it, and it is stopped and its profile deleted when the page is read.

Without Crawl4AI, pages are read with trafilatura only, and lists and tables it takes for page furniture (the plan list above) stay out.

### Local OCR

Scanned PDFs hold page images, not text. With OCR installed, webgraft renders the scanned pages (pypdfium2) and reads them with RapidOCR (PaddleOCR models in ONNX format, run by onnxruntime): no language model, no service, about 1 second per page, up to 30 pages per document.

Like Crawl4AI, it lives in its own `hermes pm` environment (`HERMES_HOME/environments/webgraft-ocr`, about 250 MB). On first use RapidOCR downloads its models (about 32 MB) from its official repository on modelscope.cn and checks each one against the SHA-256 it ships with.

Hermes's own `read_file` routes scanned pages to a vision model instead (`vision_analyze`); webgraft keeps that for what OCR cannot read, such as figures and handwriting.

## Settings

Under Settings → Plugins in Hermes Desktop, or `plugins.entries.webgraft.settings` in `config.yaml`:

| Key | Default | Meaning |
|---|---|---|
| `language` | `auto` | Footer language: `auto` (Hermes's `display.language`; English for languages other than en/fr), `answer` (the reply's language), `en` or `fr` |
| `show_sources` | `auto` | `auto`: list the opened pages, unless another plugin also rewrites replies (see below). `always`: list them anyway. `never`: no list |
| `remind` | `true` | Add a short note to each user message (about 70 tokens), marked as a note and not the request: search snippets are not sources, open the pages, and open the pages a multi-URL call left out. When the message holds a URL, the note asks to open that page instead of searching |
| `one_url_per_call` | `true` | A `web_extract` call that asks for several URLs reads only the first; the result lists the others |
| `focus` | `true` | On a page longer than what Hermes shows the model, put the passages about the question first (nothing is removed) |
| `llms_txt` | `true` | When the site's `llms.txt` lists a Markdown version of the page, read that instead of the HTML |
| `respect_ai_optout` | `true` | A page that asks not to be used by AI (`noai`, `tdm-reservation: 1`) comes back as an error |

Search and page reading are separate settings: webgraft only reads pages. It works with any search backend.

## What it does, for review

- **Hooks:**
  - `pre_llm_call` (starts a record for the turn, notes the user's message for `focus` and, unless `remind` is off, appends the short reminder to the user message)
  - `pre_tool_call` (only for `web_extract`: keeps the first URL of a call that asks for several when `one_url_per_call` is on, and notes which question the page is read for)
  - `transform_tool_result` (adds the URLs left out to the result of such a call, and a note to a `web_search` result that holds no result)
  - `post_tool_call` (keeps the text of `web_extract` results and the `web_search` query)
  - `transform_llm_output` (appends the footer; the answer itself is not changed, so a streamed answer is not printed twice)

- **Web provider:** `webgraft`, extract only.

- **CLI:** `hermes webgraft status|setup|uninstall`.

- **Skill:** `webgraft:webgraft`, read-only, loaded only if you auto-load it or call it.

- **Network:** downloads the URLs the agent asks `web_extract` to read, directly from this machine with a desktop browser user agent, and, with `llms_txt` on, each site's `/llms.txt` once per hour (plus the `.md` version of a page it lists).
  - `setup` also checks the SearXNG URL you give it and reads two test pages on iana.org; with OCR chosen, RapidOCR downloads its models once from modelscope.cn (SHA-256 checked).
  - Crawl4AI's browser loads the same URLs and their page assets.

- **Private addresses:**
  - pages, `/llms.txt` and `.md` versions are fetched with Hermes's SSRF-safe client (`tools.url_safety`), which refuses private, loopback, link-local and cloud metadata addresses on every redirect hop;
  - a result names the page actually read in `url` (the requested URL is in `metadata.sourceURL`), so Hermes re-checks a redirect target against its SSRF and website policies;
  - the browser starts only after that download, on the address it reached, and its view is dropped when the page it ended on (after its own redirects and script navigation) is not a public address.

- **Processes:**
  - one Crawl4AI child process per page, at most three at once, each stopped after 60 seconds; it starts Hermes's Chromium headless (DevTools port on 127.0.0.1, temporary profile) and stops it before exiting;
  - one child process per document (Hermes's own Python running `doc_reader.py` with anydoc), stopped after 60 seconds;
  - for a scanned PDF with OCR installed, one more child process (`ocr_runner.py` in the OCR environment), stopped after 180 seconds.
  - Children get a minimal environment (path, home and temp folders, locale, proxy settings, and what each one needs), never Hermes's API keys.

- **Writes:**
  - `web.search_backend`, `web.extract_backend`, `skills.auto_load` and, for SearXNG, `SEARXNG_URL` in `.env`, all through Hermes's own config functions and only during `setup` and `uninstall`;
  - the values `setup` replaced, in `HERMES_HOME/plugin-data/webgraft/previous.json`;
  - the Chromium path, in `browser.json` next to it.

  Nothing else is stored: page text lives in memory only, for at most 30 pages per conversation and 20 conversations, and so do the last user message and search queries of each conversation (for `focus`) and which sites publish an `llms.txt` (for an hour); all of it is gone when Hermes stops.

- **Reads:**
  - the `plugin.yaml` of other installed plugins (`name` and `provides_hooks` only, no YAML library needed), to find reply rewriters;
  - `web.extract_char_limit` and `display.language` in `config.yaml`;
  - `hermes pm env chromium` and `browser.executable_path`, during `setup`, to find Hermes's Chromium.

- **No telemetry**, no credentials read, no self-update.

## Other plugins that rewrite replies

Hermes runs every plugin's `transform_llm_output` hook but keeps only the first rewrite of a reply, so two plugins that append a footer hide one another without warning.

webgraft looks for any other enabled plugin that declares that hook: it reads the `plugin.yaml` of the plugins installed in `HERMES_HOME/plugins` (the catalog requires declared hooks to match what a plugin registers) and asks Hermes whether each one is enabled (`ctx.has_plugin`). Plugins installed with pip have no manifest there and are not seen.

With `show_sources: auto` it then adds nothing and leaves the reply to that plugin. `hermes webgraft status` names the plugin. Set `show_sources: always` to show webgraft's footer instead, or disable the other plugin. Page reading and the reminder keep working either way.

## Limits

- **What is mechanical and what is an estimate.**
  - Mechanical: HTTP errors, document conversion and OCR, `llms.txt`, AI opt-outs, one URL per call, the links of a page, the empty-search note, and the list of opened pages (from tool results).
  - Estimates: bot walls on pages that answer 200 (phrases and headers), `focus` (which passages come first), the browser-view complement (which lines hold missing values), the "opened earlier" pages (shared values), and `language: answer`.
  - Each estimate is additive, shown as such (a note, a heading), and can be turned off, except bot-wall detection, which removes a page and says why.

- The footer says which pages the answer was built from. It does not check that each sentence of the answer matches them: that would take another model call per reply and still not be reliable.

- An earlier page is matched on values with a unit, a price or a version; an answer that reuses only its prose is not linked to it.

- Pages behind logins or strong bot protection cannot be read; they come back as errors.

- Hermes refuses private and internal addresses (127.0.0.1, 10.x, 192.168.x, link-local) before any extraction backend runs: an agent reading untrusted pages must not be able to reach your network.
  - webgraft applies the same rule to redirects and to the browser (see "Private addresses" above).
  - The result is "Blocked: URL targets a private or internal network address".
  - To read pages on your own LAN, set `security.allow_private_urls: true` in Hermes's `config.yaml`; cloud metadata addresses stay blocked.

- Hermes keeps `web_extract` results for 20 minutes (`web.cache_ttl_minutes`), across sessions, and a cached page does not go through webgraft again: within that time a page keeps the order `focus` gave it for the first question.
  - Nothing is removed either way, and `web.cache_enabled: false` turns the cache off.

- `focus` matches words, not meaning: a question worded differently from the page (a synonym, another language) may move nothing, and the page then reaches the model as before.
  - The model can still page through the full text with `read_file`.

## Dependencies

Declared in `pyproject.toml`, installed by Hermes inside its own constraints:

| Package | Range | Floor chosen because |
|---|---|---|
| trafilatura | `>=1.10,<3` | oldest release that accepts lxml 6, which Hermes pins (tested 1.10 to 2.3) |
| rapidocr, onnxruntime, pypdfium2 | `>=3.10,<4`, `>=1.20,<2`, `>=5,<6` | separate OCR environment, installed by `setup`; tested with RapidOCR 3.10, onnxruntime 1.31 and Python 3.14 |
| crawl4ai | `>=0.9.1,<0.10` | separate environment, installed by `setup`; 0.9.0 and older need lxml 5, which has no Python 3.14 wheels (tested 0.9.1 to 0.9.5) |

## Development

```
python tools/check.py
```

Runs everything locally, no cloud CI:

- for Python 3.11 and 3.14, `uv` builds a throwaway environment with the dependencies from `pyproject.toml` (only releases at least 3 days old, the release-age quarantine the Hermes plugin guide recommends) and runs the tests;
- then the Hermes on the machine runs `hermes plugins doctor . --ci` (loads the plugin the way Hermes does) and `hermes plugins validate . --install-deps` (the catalog CI check).

`--python 3.12` tests other versions, `--skip-hermes` runs the tests only.

The tests use made-up pages and need no network; the document tests start a child Python.

Documents are read with anydoc (`firecrawl-anydoc`), which Hermes provides, so it is not a plugin dependency; `tools/check.py` installs it in the test environment.

Built for the Nous Research Hermes hackathon (November 2026).

## License

MIT
