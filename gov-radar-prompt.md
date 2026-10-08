# One-shot prompt: Gov Radar v1

Paste everything below the line into Claude Code in an empty folder.

---

Build a project called **gov-radar**: a daily tool that pulls new US government documents about (a) AI and tech policy and (b) defense spending, summarizes and tags each one, links documents that share hard facts, and shows the result as a static web page with a cluster map.

I am an IT person, not a full-time developer. Explain your choices in plain English in the README. Build the whole thing in one pass, then run it once end to end and fix what breaks.

## Before writing code

Check that each data source below still responds and returns the shape you expect. If an endpoint has moved or changed, find the current one and note the change in the README. Do not guess field names; fetch a real sample first.

## Data sources (v1: these two only)

1. **Federal Register API** (no key needed)
   - Base: `https://www.federalregister.gov/api/v1/documents.json`
   - Pull documents published in the last 2 days (overlap on purpose so nothing is missed; dedupe by document number).
   - Run one search per keyword below using the search-term parameter, and keep the union.
2. **GAO reports RSS feed**
   - Start from `https://www.gao.gov/rss/reports.xml`
   - Keep only items whose title or summary matches a keyword below.

Do NOT add more sources in v1. Put USAspending.gov contract data in the README under "Phase 2".

## Keyword lists (pre-baked, keep in `config.yaml`)

**Lane: ai_tech**
artificial intelligence, machine learning, generative AI, large language model, autonomous systems, semiconductor, export controls, advanced computing, data center, cybersecurity, quantum, algorithmic

**Lane: defense_spending**
Department of Defense, defense acquisition, procurement, NDAA, National Defense Authorization Act, weapon system, defense contract, DARPA, defense budget, unmanned, munitions, shipbuilding, Defense Innovation Unit

A document can belong to both lanes. Documents in both lanes are the most interesting ones, so flag them as `crossover`.

## Pipeline (one command: `python run.py`)

1. **Fetch** new documents from both sources. Store raw records in SQLite (`radar.db`). Skip anything already stored.
2. **Get the text.** Use the abstract or summary plus the first ~6,000 characters of body text if available. Do not send whole documents to the model.
3. **Summarize and tag** each new document with the Anthropic API.
   - Model: `claude-haiku-4-5-20251001` (set in `config.yaml` so I can change it).
   - API key from the `ANTHROPIC_API_KEY` environment variable.
   - Ask for strict JSON: `summary` (2 sentences, plain English), `agencies`, `programs`, `statutes` (laws, CFR parts, executive orders), `dollar_amounts`, `doc_type`, and for every entity a `quote` field containing the exact words from the source text that mention it.
   - **Validation rule:** after the model replies, check in code that each `quote` really appears in the source text. Drop any entity whose quote is not found. This stops the model from inventing connections.
4. **Normalize entities** so "DoD", "Department of Defense", and "Defense Department" become one entity. Keep an alias table in `config.yaml` with a starter set for the major agencies; log unknown entities so I can add aliases later.
5. **Embed** each summary locally with `sentence-transformers` model `all-MiniLM-L6-v2` (free, runs offline). Store vectors in SQLite. Use them only for a "similar documents" list, never to create links on the map.
6. **Link.** Two documents are linked only if they share at least one normalized program or statute, OR share an agency AND a dollar amount. Agency alone is not a link (everything would connect to everything). Store each link with the reason and both quotes.
7. **Cluster.** Group linked documents into connected clusters. Name each cluster after its most common shared program or statute. Rank clusters by number of documents added in the last 30 days.
8. **Export** `site/data.json` with documents, links, and the top 50 clusters.

## Web page (`site/index.html`, static, no build step)

- One self-contained HTML file plus `data.json`. Must work on GitHub Pages and on a phone.
- Top: a ranked list of the top clusters with document count and a "new this week" count.
- Middle: a force-directed map (vis-network or D3 from a CDN). Nodes are documents, colored by lane, with crossover documents in a third color. Edges are links.
- Tapping a document shows its summary, source link, tags, and similar documents.
- Tapping an edge shows WHY the two are linked, with the exact quote from each document.
- A plain banner at the top: "Shows documents that share programs, laws, or funding. It does not show intent or plans."

## Guardrails

- Never have the model speculate about what the government is "really" doing. Summaries describe the document only.
- No link without a quoted, verified reason.
- Print a cost estimate at the end of each run (documents processed, tokens used).
- Cap each run at 150 new documents; if more come in, process the newest and log the rest as skipped.

## Deliverables

- `run.py`, `config.yaml`, `requirements.txt`, `site/index.html`, `README.md`
- `.github/workflows/daily.yml` that runs the pipeline once a day and commits `site/data.json` (disabled by default, with README steps to turn it on and add the API key as a repo secret).
- README in plain language covering: what each step does and WHY it exists, how to run it, how to add a keyword or alias, rough daily cost, known limits, and the Phase 2 list (USAspending contracts, Congress.gov bills, weekly email digest).
- Finish by running the pipeline once on live data and telling me how many documents, links, and clusters it produced.
