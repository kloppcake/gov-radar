# Gov Radar

A small daily tool that:

1. pulls new US government documents about **AI/tech policy** and **defense spending**,
2. has an AI model write a 2-sentence summary and pull out agencies, programs, laws, and dollar amounts,
3. **links documents only when they share hard facts** (and shows the exact quotes proving it),
4. shows the result as a static web page with a cluster map.

The page carries this banner: *"Shows documents that share programs, laws, or funding. It does not show intent or plans."* That is the honest limit of the tool. A link means two documents mention the same thing. It does not mean anyone is coordinating anything.

## Quick start

```bash
pip install -r requirements.txt
# Windows PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."
# Mac/Linux:           export ANTHROPIC_API_KEY=sk-ant-...
python run.py
python -m http.server -d docs 8000     # then open http://localhost:8000
```

(The page needs to be served, not double-clicked, because browsers block a local file from reading `data.json`. On GitHub Pages this just works.)

The first run downloads the small embedding model (~90 MB) once.

`python run.py --no-fetch` skips downloading and rebuilds links/clusters/the site from what is already in `radar.db`. Use it after editing aliases.

## What each step does, and why it exists

| # | Step | Why it exists |
|---|------|---------------|
| 1 | **Fetch** from the Federal Register API (one search per keyword, last 2 days) and the GAO reports RSS feed. Raw records go into `radar.db` (SQLite). Anything already stored is skipped. | The 2-day window overlaps on purpose so a late-published document is never missed; the database makes the overlap harmless. A cap of 150 new documents per run protects your API bill. Extras are listed in `skipped.log`. |
| 2 | **Get the text**: abstract + first ~6,000 characters of the body (GAO: the feed's summary). | Models charge per word read. The opening of a document says what it is about; the rest is rarely needed. |
| 3 | **Summarize and tag** with Claude Haiku. The model must return JSON, and **every entity must come with an exact quote**. Code then checks that each quote really appears in the text. If not, the entity is thrown away. | Language models sometimes invent plausible details. Links built on invented details would be worse than no links. This check is the safety net. |
| 4 | **Normalize** names with the alias table in `config.yaml`: "DoD", "Defense Department", and "Department of Defense" become one entity. Executive orders, CFR parts, U.S. Code sections, and public laws are standardized by pattern. Unrecognized names go to `unknown_entities.txt`. | Without this, the same agency written three ways would never match. Normalization happens when links are built, not when stored, so adding an alias takes effect on the next run with no re-summarizing. |
| 5 | **Embed** each summary on your own machine (`all-MiniLM-L6-v2`, free, offline). | Powers the "similar documents" list only. Similarity is fuzzy, so it never creates a link on the map. |
| 6 | **Link.** Two documents are linked only if they share a program or a law, **or** share an agency **and** a dollar amount. Agency alone is not enough. Each link stores the reason and both quotes. | "Everyone mentions the Department of Defense" would connect everything to everything. Requiring a specific shared fact keeps the map meaningful. |
| 7 | **Cluster.** Linked documents are grouped with Louvain community detection (a standard method that keeps tightly linked groups together and cuts weak bridges). Simply grouping everything that is connected at all turned 640 unrelated documents into one blob once the library grew. A cluster is named after its most common shared program or law and ranked by documents published in the last 30 days. | Gives you a short list of "what topics have fresh activity". |
| 8 | **Export** `docs/data.json` (documents, links, top 50 clusters). | The web page is just a viewer for this one file. |

### Guardrails built in
- The prompt tells the model to describe the document only, never to guess at intent.
- No link exists without a quote from both documents that passed the verification check.
- Dollar amounts are double-checked: the number in the quote must equal the number the model reported.
- Each run prints documents processed, tokens used, and an estimated cost.

### Choices I made that you should know about
- **Federal Register keyword filter.** The API's keyword search matches words *anywhere* in a document. On a real sample, an Alzheimer's advisory meeting and a transit committee notice matched "Department of Defense" and "procurement" from incidental mentions. So after searching, a document keeps a lane only if one of that lane's keywords appears in its **title or abstract** (or, when there is no abstract, its opening text). This cuts the Federal Register results by roughly two-thirds on the samples I checked, and every remaining document is actually about the topic. To go back to raw full-text matching, remove the call to `confirm_fr_lanes` in `run.py`.
- **Boilerplate laws are tagged but never linked.** The Administrative Procedure Act, Paperwork Reduction Act, and similar appear in nearly every Federal Register document. Showing them as tags is fine; using them as links would join everything. The list is `boilerplate_entities` in `config.yaml`.
- **Too-generic shared items are skipped.** If one program or law is shared by more than 40 documents, `run.py` logs it and does not link on it.
- **Small dollar amounts are ignored for linking** (under $1,000,000, set in `config.yaml`), because figures like $5,000 coincide by accident.
- **Year-specific laws stay separate.** "NDAA for Fiscal Year 2024" and "...2025" are different laws, so they are different entities. If you want them merged, add an alias.

## Data sources (checked live before building)

- **Federal Register API**: `https://www.federalregister.gov/api/v1/documents.json`, no key needed. Confirmed response fields: `document_number`, `title`, `abstract` (can be `null`, e.g. presidential documents), `html_url`, `publication_date`, `type`, `agency_names`, `raw_text_url`. Nothing had moved. One practical note: search terms must be URL-encoded, which the code does.
- **GAO reports RSS**: `https://www.gao.gov/rss/reports.xml`, unchanged. Items have `title`, `link`, `description` (HTML, starting with "What GAO Found"), `pubDate`, `guid`. The feed holds only the ~25 most recent reports, so it is only useful if you run daily.

## How to add a keyword or an alias

Everything is in `config.yaml`.

- **Keyword:** add a line under `lanes:` → `ai_tech` or `defense_spending`. A document matching both lanes is flagged `crossover`.
- **Alias:** open `unknown_entities.txt` after a run. If two lines are the same thing, add them under `aliases:` in the right group:
  ```yaml
  aliases:
    agencies:
      Department of Defense: [DoD, Defense Department, Pentagon]
  ```
  Then run `python run.py --no-fetch`. No new API cost.
- **Stop a law from linking everything:** add it to `boilerplate_entities`.

## The daily run (GitHub Actions)

The workflow in `.github/workflows/daily.yml` runs every day at 11:17 UTC and can also be started by hand (**Actions** tab → "Gov Radar daily" → **Run workflow**). It needs one thing from you:

1. On GitHub: **Settings → Secrets and variables → Actions → New repository secret**.
2. Name: `ANTHROPIC_API_KEY`. Secret: your key. Save.
3. Run the workflow once by hand to test it.

If the secret is missing, the run stops with a clear error instead of quietly doing nothing. To pause the automation, **Actions → Gov Radar daily → ⋯ → Disable workflow**.

After each run it commits `docs/data.json` (what the web page reads) **and** `radar.db` (the memory of what has already been processed) back to `main`. Without the database every run would start from zero. The database grows slowly (a few KB per document). The web page is published by GitHub Pages from the `/docs` folder on `main`.

## Backfilling history

`python run.py --since 2024-10-08 --until 2025-03-31 --max-docs 800` fetches an older date range (Federal Register only; GAO's feed has no archive, so GAO builds up from the day you start). On GitHub use **Actions → Gov Radar daily → Run workflow** and fill in the dates (leave blank for a normal run). Do about six months per run, one run at a time. The two years from Oct 2024 were loaded this way for roughly $4.50 total.

## Rough daily cost

I have **not** been able to measure this on real model output yet (see "Status" below), so treat these as estimates. Per document the model reads roughly 2,000 tokens and writes roughly 500. At Haiku 4.5 prices ($1 / $5 per million tokens) that is about **$0.005 per document**. With the 2-day Federal Register window plus GAO, a typical weekday should be tens of documents, so **roughly $0.10 to $0.30 per day**, and **at most about $0.75 per day** because of the 150-document cap. The embedding step is free. Each run prints its own token count and cost; compare that with these numbers after your first real run and update the prices in `config.yaml` if they differ.

## Known limits

- **Links mean "mentions the same thing", not "is related in intent".** Two documents citing the same law may have nothing else in common.
- **The model can still get things wrong in ways quotes can't catch**, for example choosing a poor summary emphasis or attaching a real quote to the wrong entity name. The quote check only proves the words exist in the document.
- Only the first ~6,000 characters of each Federal Register document are read. Facts later in long documents are missed.
- GAO text is only the feed's short "What GAO Found" summary, not the full report.
- GAO's feed shows only its newest ~25 reports. Days you don't run are lost for GAO.
- The Federal Register filter requires a keyword in the title/abstract, so a relevant document that only discusses the topic deep in its body will be missed.
- Weekends: the Federal Register doesn't publish, so a Monday run with a 2-day window mostly sees Monday's documents.
- Name matching is only as good as your alias table. Unrecognized variants silently fail to link until you add aliases.
- The cluster map is a visual aid. With many documents, use the cluster list to focus it.

## Status

Built and tested on 2026-10-05. Fetching (both sources), text prep, quote validation, normalization, local embeddings, linking, clustering, export, and the web page were all exercised on live Federal Register and GAO data. The model call itself was **not** run against the real Anthropic API in that session because no `ANTHROPIC_API_KEY` was available; a stand-in summarizer was used to test everything after it. Your first real run is therefore the first true test of the prompt. Look at `unknown_entities.txt` and a few cluster pop-ups, and tune the prompt in `run.py` (`USER_PROMPT`) if the tags look off.

## Phase 2 (not built yet)

- **USAspending.gov contracts**: link documents to real contract awards by program and dollar amount.
- **Congress.gov bills**: add bills and their text as a third source.
- **Weekly email digest** of the top clusters and "new this week" items.

## Files

| File | What it is |
|------|-----------|
| `run.py` | The whole pipeline. Each step is one function, in order. |
| `config.yaml` | Keywords, model name, prices, link rules, boilerplate list, alias table. |
| `requirements.txt` | Python packages. |
| `docs/index.html` | The viewer (one file; loads vis-network from a CDN). |
| `docs/data.json` | Generated output the page reads. |
| `radar.db` | SQLite memory of everything fetched. Safe to delete to start over. |
| `unknown_entities.txt`, `skipped.log` | Generated: names to alias, and documents over the daily cap. |
| `.github/workflows/daily.yml` | Optional daily automation (off by default). |
