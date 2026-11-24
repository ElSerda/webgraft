# Hermes catalog entry

The entry for webgraft in Hermes's plugin catalog (`plugin-catalog/webgraft.yaml` in NousResearch/hermes-agent), prepared here before each re-pin PR.

- **webgraft.yaml** is the entry to submit. Before the PR, set `sha` to the commit you pin and `version` to the version in `plugin.yaml`, then check it:

  ```
  bash tools/catalog_ci.sh hermes-catalog/webgraft.yaml
  ```

- **The description** keeps its line breaks: a short intro (the part a card shows, 400 characters at most), then lines starting with "- ". The api-indonesia entry has the same layout, and the maintainers kept it in review.
- **The disclosure** is written by the maintainers in review, after "Disclosure —". The full list of what webgraft downloads, runs and stores is in the main README, "What it does, for review". The PR text gives the short version below and points to that section.

## Disclosure for the PR text

- **Network:** downloads the URLs it is asked to read, and each site's /llms.txt once per hour, through Hermes's SSRF-safe client. A page's own scripts and assets in the browser are not checked.
- **Processes:** page readers (Crawl4AI with Hermes's Chromium, anydoc, optional OCR) run as capped child processes without Hermes's API keys. The OCR's first run downloads about 32 MB of models, checked by SHA-256.
- **Settings:** `setup` writes the web.* settings, skills.auto_load and SEARXNG_URL. `uninstall` restores them.
- **Stored:** only conversation and Kanban card ids, no content, under plugin-data/webgraft.
- **Context:** an llm_request middleware removes the sources lists from earlier answers before each model call, so they never take up the model's context.
- No page text kept, no telemetry.
