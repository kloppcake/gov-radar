#!/usr/bin/env python
"""Gov Radar: fetch -> text -> summarize/tag -> normalize -> embed -> link -> cluster -> export.

Run with:  python run.py
Options:   python run.py --no-fetch    (skip downloading; just rebuild links/clusters/docs/data.json from radar.db)

Every step is a function below, in the order it runs. See README.md for the plain-English explanation.
"""
import argparse
import collections
import datetime as dt
import email.utils
import hashlib
import html
import math
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
FR_URL = "https://www.federalregister.gov/api/v1/documents.json"
GAO_URL = "https://www.gao.gov/rss/reports.xml"
FR_FIELDS = ["document_number", "title", "abstract", "html_url", "publication_date",
             "type", "agency_names", "raw_text_url"]
UA = {"User-Agent": "gov-radar/1.0 (personal research tool)"}
MAX_ATTEMPTS = 3          # a document that fails summarizing this many times is left alone
ENTITY_TYPES = ["agencies", "programs", "statutes", "dollar_amounts"]

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- config / db

def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def open_db(cfg):
    db = sqlite3.connect(ROOT / cfg["db_path"])
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY,          -- 'fr:<document number>' or 'gao:<report id>'
        source TEXT, title TEXT, url TEXT, pub_date TEXT,
        lanes TEXT,                   -- JSON list: ai_tech / defense_spending
        raw TEXT,                     -- the record exactly as the source gave it (JSON)
        text TEXT,                    -- the text sent to the model (also used to verify quotes)
        status TEXT DEFAULT 'pending',-- pending / done
        attempts INTEGER DEFAULT 0,
        summary TEXT, doc_type TEXT,
        entities TEXT,                -- JSON of validated entities (name + verified quote), before alias normalization
        embedding BLOB,
        first_seen TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS ai_text (    -- cache of AI-written cluster/pair/group text
        hash TEXT PRIMARY KEY,        -- sha256 of (model + prompt): unchanged input = no new API call
        kind TEXT, text TEXT, created TEXT)""")
    db.commit()
    return db


# --------------------------------------------------------------------------- text helpers

def strip_html(s):
    s = html.unescape(html.unescape(s or ""))
    s = re.sub(r"<(script|style).*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


_QUOTE_MAP = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                            "–": "-", "—": "-", "−": "-", " ": " "})


def canon_text(s):
    """Loose form used only to check that a quote really appears in the source."""
    s = unicodedata.normalize("NFKC", s or "").translate(_QUOTE_MAP).casefold()
    return re.sub(r"\s+", " ", s).strip()


def kw_regex(kw):
    return re.compile(r"(?<!\w)" + re.escape(kw) + r"(?:s|es)?(?!\w)", re.I)


# --------------------------------------------------------------------------- step 1: fetch

def http_get(url, **kw):
    last = None
    for i in range(6):
        wait = 2 * (i + 1)
        try:
            r = requests.get(url, headers=UA, timeout=45, **kw)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code == 429:         # rate limited: back off for real, honouring Retry-After
                try:
                    wait = max(int(r.headers.get("Retry-After", 0)), 15 * (i + 1))
                except ValueError:
                    wait = 15 * (i + 1)
        except requests.RequestException as e:
            last = str(e)
        time.sleep(wait)
    raise RuntimeError(f"GET {url} failed: {last}")


def fr_body_text(raw_text_url):
    """Plain body text of a Federal Register document, without the GPO header block."""
    time.sleep(0.4)                  # be polite: the Federal Register rate-limits rapid downloads
    t = strip_html(http_get(raw_text_url).text)
    m = re.search(r"={5,}", t[:2500])
    if m:
        t = t[m.end():]
    return re.sub(r"\[\[Page \d+\]\]", " ", t).strip()


def confirm_fr_lanes(cfg, found):
    """The Federal Register search matches words anywhere in a document, which is noisy (an Alzheimer's
    advisory meeting can match 'Department of Defense' in a footnote). Keep a lane only if one of its keywords
    appears in the title or abstract, or in the opening text when there is no abstract."""
    patterns = {lane: [kw_regex(k) for k in kws] for lane, kws in cfg["lanes"].items()}

    def check(d):
        rec = d["raw"]
        if rec.get("abstract"):
            hay = d["title"] + " " + rec["abstract"]
        else:
            try:
                rec["_body"] = fr_body_text(rec["raw_text_url"])
            except Exception:
                rec["_body"] = ""
            hay = d["title"] + " " + rec["_body"][:cfg["body_chars"]]
        d["lanes"] = {l for l in d["lanes"] if any(p.search(hay) for p in patterns[l])}

    with ThreadPoolExecutor(2) as ex:
        list(ex.map(check, found.values()))
    return {k: d for k, d in found.items() if d["lanes"]}


def date_windows(since, until):
    """Split a date range into month-sized pieces so no single search hits the API's result limit."""
    out, a = [], dt.date.fromisoformat(since)
    end = dt.date.fromisoformat(until)
    while a <= end:
        b = min(a + dt.timedelta(days=30), end)
        out.append((a.isoformat(), b.isoformat()))
        a = b + dt.timedelta(days=1)
    return out


def fetch_federal_register(cfg):
    since = cfg.get("_since") or (dt.date.today() - dt.timedelta(days=cfg["fetch_days"])).isoformat()
    windows = date_windows(since, cfg["_until"]) if cfg.get("_until") else [(since, None)]
    found = {}
    searches = [(lane, kw, w) for lane, kws in cfg["lanes"].items() for kw in kws for w in windows]
    for lane, kw, (w_from, w_to) in searches:
        page, total = 1, 1
        while page <= total and page <= 10:
            params = [("conditions[term]", f'"{kw}"'),
                      ("conditions[publication_date][gte]", w_from),
                      ("per_page", 100), ("page", page), ("order", "newest")]
            if w_to:
                params.append(("conditions[publication_date][lte]", w_to))
            params += [("fields[]", f) for f in FR_FIELDS]
            data = http_get(FR_URL, params=params).json()
            total = data.get("total_pages", 1) or 1
            for rec in data.get("results", []):
                d = found.setdefault("fr:" + rec["document_number"], {
                    "id": "fr:" + rec["document_number"], "source": "federal_register",
                    "title": rec.get("title") or "", "url": rec.get("html_url"),
                    "pub_date": rec.get("publication_date"), "lanes": set(), "raw": rec})
                d["lanes"].add(lane)
            page += 1
        time.sleep(0.2)
    hits = len(found)
    found = confirm_fr_lanes(cfg, found)
    log(f"  Federal Register: {hits} full-text hits since {since}, {len(found)} with a keyword in the title/abstract")
    return list(found.values())


def fetch_gao(cfg):
    root = ET.fromstring(http_get(GAO_URL).content)
    patterns = {lane: [kw_regex(k) for k in kws] for lane, kws in cfg["lanes"].items()}
    out = []
    items = root.findall("./channel/item")
    for it in items:
        title = (it.findtext("title") or "").strip()
        desc = it.findtext("description") or ""
        haystack = title + " " + strip_html(desc)
        lanes = {lane for lane, pats in patterns.items() if any(p.search(haystack) for p in pats)}
        if not lanes:
            continue
        guid = (it.findtext("guid") or it.findtext("link") or title).strip()
        try:
            pub = email.utils.parsedate_to_datetime(it.findtext("pubDate")).date().isoformat()
        except Exception:
            pub = dt.date.today().isoformat()
        rid = guid.rstrip("/").split("/")[-1]
        out.append({"id": "gao:" + rid, "source": "gao", "title": title,
                    "url": (it.findtext("link") or "").strip(), "pub_date": pub, "lanes": lanes,
                    "raw": {"title": title, "link": it.findtext("link"), "pubDate": it.findtext("pubDate"),
                            "guid": guid, "description": desc}})
    log(f"  GAO: {len(items)} items in feed, {len(out)} match keywords")
    return out


def step_fetch(cfg, db):
    log("Step 1: fetch")
    records = []
    sources = [("Federal Register", fetch_federal_register)]
    if not cfg.get("_until"):        # GAO's feed has no archive, so it is skipped when backfilling
        sources.append(("GAO", fetch_gao))
    for name, fn in sources:
        try:
            records += fn(cfg)
        except Exception as e:      # one broken source should not stop the other
            log(f"  WARNING: {name} failed: {e}")
    have = {r[0] for r in db.execute("SELECT id FROM documents")}
    new = [r for r in records if r["id"] not in have]
    new.sort(key=lambda r: r["pub_date"] or "", reverse=True)
    cap = cfg["max_new_docs_per_run"]
    keep, skipped = new[:cap], new[cap:]
    now = dt.datetime.now().isoformat(timespec="seconds")
    for r in keep:
        db.execute("INSERT INTO documents (id, source, title, url, pub_date, lanes, raw, first_seen) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (r["id"], r["source"], r["title"], r["url"], r["pub_date"],
                    json.dumps(sorted(r["lanes"])), json.dumps(r["raw"]), now))
    db.commit()
    if skipped:
        with open(ROOT / "skipped.log", "a", encoding="utf-8") as f:
            for r in skipped:
                f.write(f"{now}\tskipped (over {cap}/run cap)\t{r['id']}\t{r['title']}\n")
    log(f"  {len(new)} new documents; stored {len(keep)}, skipped {len(skipped)} (see skipped.log)")
    return len(keep)


# --------------------------------------------------------------------------- step 2: text

def build_fr_text(cfg, row):
    raw = json.loads(row["raw"])
    body = raw.get("_body")
    if body is None and raw.get("raw_text_url"):
        try:
            body = fr_body_text(raw["raw_text_url"])
        except Exception as e:
            log(f"  could not fetch body for {row['id']}: {e}")
    return f"TITLE: {row['title']}\nABSTRACT: {raw.get('abstract') or '(none)'}\nBODY: {(body or '')[:cfg['body_chars']]}"


def build_gao_text(cfg, row):
    raw = json.loads(row["raw"])
    desc = strip_html(raw.get("description"))
    return f"TITLE: {row['title']}\nABSTRACT/SUMMARY: {desc[:cfg['body_chars'] + 1500]}"


def step_text(cfg, db):
    log("Step 2: get text")
    rows = db.execute("SELECT * FROM documents WHERE text IS NULL").fetchall()

    def work(row):
        return row["id"], (build_fr_text if row["source"] == "federal_register" else build_gao_text)(cfg, row)

    with ThreadPoolExecutor(2) as ex:
        for did, text in ex.map(work, rows):
            db.execute("UPDATE documents SET text=? WHERE id=?", (text, did))
    db.commit()
    log(f"  text prepared for {len(rows)} documents")


# --------------------------------------------------------------------------- step 3: summarize + tag

SYSTEM_PROMPT = (
    "You extract structured facts from ONE United States government document. "
    "Describe only what the document says. Never speculate about motives, plans, or what the "
    "government is 'really' doing. Reply with a single JSON object and nothing else.")

USER_PROMPT = """Read the document below and return JSON with exactly these keys:

{{
  "summary": "Exactly 2 plain-English sentences describing what this document is and what it says.",
  "doc_type": "one of: rule, proposed rule, notice, presidential document, audit report, other",
  "agencies":       [{{"name": "...", "quote": "..."}}],
  "programs":       [{{"name": "...", "quote": "..."}}],
  "statutes":       [{{"name": "...", "quote": "..."}}],
  "dollar_amounts": [{{"name": "$1.2 billion", "quote": "..."}}]
}}

Rules:
- Every entity needs a "quote": the EXACT words copied from the document that mention it (contiguous, at most 200 characters, no paraphrasing, no "..." inside). If you cannot copy exact words, leave the entity out.
- agencies: government bodies named in the text (use the full name if the text gives it).
- programs: specifically named programs, initiatives, systems, funds, or contract vehicles. Not generic nouns.
- statutes: specific laws, CFR parts, U.S. Code sections, public laws, and executive orders (e.g. "Executive Order 14110", "15 CFR Part 744").
- dollar_amounts: specific dollar figures the document states (e.g. "$850 million"). "name" is the amount as written.
- Use empty lists when nothing qualifies. Do not invent anything.

DOCUMENT:
{text}
"""


def parse_json_reply(txt):
    txt = txt.strip()
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt)
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j < 0:
        raise ValueError("no JSON object in reply")
    return json.loads(txt[i:j + 1])


_AMT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion|trillion)?", re.I)
_AMT2 = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion|trillion)\s+dollars", re.I)
_MULT = {None: 1, "": 1, "thousand": 10 ** 3, "million": 10 ** 6, "billion": 10 ** 9, "trillion": 10 ** 12}


def parse_amounts(s):
    out = set()
    for rx in (_AMT, _AMT2):
        for num, unit in rx.findall(s or ""):
            try:
                out.add(int(round(float(num.replace(",", "")) * _MULT[(unit or "").lower() or None])))
            except ValueError:
                pass
    return out


def validate_entities(parsed, source_text):
    """The key guardrail: keep an entity only if its quote really appears in the source text."""
    hay = canon_text(source_text)
    kept, dropped = {t: [] for t in ENTITY_TYPES}, 0
    for t in ENTITY_TYPES:
        for e in parsed.get(t) or []:
            if not isinstance(e, dict):
                dropped += 1
                continue
            name, quote = str(e.get("name") or "").strip(), str(e.get("quote") or "").strip()
            ok = len(name) > 1 and len(quote) >= 3 and canon_text(quote) in hay
            if ok and t == "dollar_amounts":
                want = parse_amounts(name)
                ok = bool(want) and want <= parse_amounts(quote)
            if ok:
                kept[t].append({"name": name, "quote": quote})
            else:
                dropped += 1
    return kept, dropped


def summarize_one(client, cfg, row):
    prompt = USER_PROMPT.format(text=row["text"])
    for attempt in range(2):
        resp = client.messages.create(model=cfg["model"], max_tokens=2000,
                                      system=SYSTEM_PROMPT, messages=[{"role": "user", "content": prompt}])
        usage = (resp.usage.input_tokens, resp.usage.output_tokens)
        try:
            parsed = parse_json_reply("".join(b.text for b in resp.content if b.type == "text"))
            return parsed, usage
        except (ValueError, json.JSONDecodeError):
            continue
    return None, usage


def step_summarize(cfg, db, stats):
    log("Step 3: summarize and tag")
    rows = db.execute("SELECT * FROM documents WHERE status='pending' AND text IS NOT NULL AND attempts<? "
                      "ORDER BY pub_date DESC", (MAX_ATTEMPTS,)).fetchall()
    if not rows:
        log("  nothing to summarize")
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log(f"  ANTHROPIC_API_KEY is not set, so {len(rows)} documents stay 'pending' until it is.")
        return
    import anthropic
    client = anthropic.Anthropic()

    def work(row):
        try:
            return row, *summarize_one(client, cfg, row), None
        except Exception as e:
            return row, None, (0, 0), e

    done = 0
    stats["summary_attempted"] = len(rows)
    with ThreadPoolExecutor(cfg["llm_workers"]) as ex:
        for row, parsed, (tin, tout), err in ex.map(work, rows):
            stats["tokens_in"] += tin
            stats["tokens_out"] += tout
            db.execute("UPDATE documents SET attempts=attempts+1 WHERE id=?", (row["id"],))
            if parsed is None or not str(parsed.get("summary") or "").strip():
                log(f"  failed {row['id']}: {err or 'unusable reply'}")
                db.commit()
                continue
            ents, dropped = validate_entities(parsed, row["text"])
            stats["entities_dropped"] += dropped
            stats["entities_kept"] += sum(len(v) for v in ents.values())
            db.execute("UPDATE documents SET status='done', summary=?, doc_type=?, entities=? WHERE id=?",
                       (str(parsed["summary"]).strip(), str(parsed.get("doc_type") or "other"),
                        json.dumps(ents), row["id"]))
            db.commit()
            done += 1
    stats["docs_summarized"] = done
    log(f"  summarized {done}/{len(rows)}; kept {stats['entities_kept']} entities, "
        f"dropped {stats['entities_dropped']} whose quote was not found in the source")


# --------------------------------------------------------------------------- step 4: normalize

def norm(s):
    s = unicodedata.normalize("NFKC", s or "").casefold().translate(_QUOTE_MAP)
    s = re.sub(r"\([^)]*\)", " ", s)                       # drop "(DoD)" style parentheticals
    s = s.replace("&", " and ")
    s = re.sub(r"\bu\.\s?s\.(?!\s?c)|\bunited states\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"^the ", "", s)


_EO = re.compile(r"(?:executive order|exec\.? order|\be\.?o\.?)\s*(?:no\.?\s*)?(\d{4,5})", re.I)
_CFR = re.compile(r"(\d+)\s*C\.?F\.?R\.?\s*(?:parts?|§+)?\s*(\d+)", re.I)
_USC = re.compile(r"(\d+)\s*U\.?S\.?C\.?\s*(?:§+\s*)?(\d+[a-z]?)", re.I)
_PL = re.compile(r"(?:public law|pub\.?\s?l\.?|\bp\.?l\.?)\s*(?:no\.?\s*)?(\d+)\s*[-–]\s*(\d+)", re.I)


class Normalizer:
    def __init__(self, cfg):
        self.index = {t: {} for t in ("agencies", "programs", "statutes")}
        for t, table in (cfg.get("aliases") or {}).items():
            for canon, aliases in (table or {}).items():
                for a in [canon] + list(aliases or []):
                    self.index[t][norm(a)] = canon
        self.boiler = {norm(b) for b in cfg.get("boilerplate_entities", [])}
        self.boiler_pat = [re.compile(p) for p in cfg.get("boilerplate_patterns", [])]
        self.min_amount = cfg["min_link_dollar_amount"]
        self.unknown = collections.Counter()

    def one(self, etype, e):
        """Return a dict with key (used for matching) and label (shown to people)."""
        name = e["name"]
        out = {"type": etype, "name": name, "quote": e["quote"]}
        if etype == "dollar_amounts":
            amts = parse_amounts(name)
            amt = max(amts) if amts else 0
            out.update(key=str(amt), label=name, amount=amt, known=True)
            return out
        canon = self.index[etype].get(norm(name))
        if canon:
            out.update(key=norm(canon), label=canon, known=True)
            return out
        if etype == "statutes":
            for rx, fmt_key, fmt_label in (
                    (_EO, "executive order {0}", "Executive Order {0}"),
                    (_PL, "public law {0} {1}", "Public Law {0}-{1}"),
                    (_CFR, "{0} cfr part {1}", "{0} CFR Part {1}"),
                    (_USC, "{0} usc {1}", "{0} U.S.C. {1}")):
                m = rx.search(name)
                if m:
                    g = [x.lower() for x in m.groups()]
                    out.update(key=fmt_key.format(*g), label=fmt_label.format(*m.groups()), known=True)
                    return out
        out.update(key=norm(name), label=name, known=False)
        self.unknown[(etype, out["key"], name)] += 1
        return out

    def is_boilerplate(self, e):
        return e["type"] == "statutes" and (e["key"] in self.boiler or any(p.search(e["key"]) for p in self.boiler_pat))

    def doc_entities(self, raw_entities):
        res = {}
        for t in ENTITY_TYPES:
            seen, lst = set(), []
            for e in raw_entities.get(t, []):
                n = self.one(t, e)
                if n["key"] and n["key"] not in seen:
                    seen.add(n["key"])
                    n["boilerplate"] = self.is_boilerplate(n)
                    lst.append(n)
            res[t] = lst
        return res


def write_unknown_entities(cfg, norm_):
    path = ROOT / "unknown_entities.txt"
    lines = ["# Names the model found that are not in the alias table (config.yaml).",
             "# If two lines are really the same thing, add them under 'aliases:'. Format: type | count | name", ""]
    order = {"agencies": 0, "programs": 1, "statutes": 2}
    merged = collections.Counter()
    for (t, key, name), c in norm_.unknown.items():
        merged[(t, name)] += c
    for (t, name), c in sorted(merged.items(), key=lambda kv: (order.get(kv[0][0], 9), -kv[1], kv[0][1])):
        if t in order:
            lines.append(f"{t} | {c} | {name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(merged)


# --------------------------------------------------------------------------- step 5: embed

def step_embed(cfg, db):
    log("Step 5: embed summaries (local model)")
    rows = db.execute("SELECT id, title, summary FROM documents WHERE status='done' AND embedding IS NULL").fetchall()
    if not rows:
        log("  nothing new to embed")
        return
    from sentence_transformers import SentenceTransformer
    import numpy as np
    model = SentenceTransformer(cfg["embedding_model"])
    vecs = model.encode([f"{r['title']}. {r['summary']}" for r in rows], normalize_embeddings=True,
                        show_progress_bar=False)
    for r, v in zip(rows, vecs):
        db.execute("UPDATE documents SET embedding=? WHERE id=?", (np.asarray(v, dtype="float32").tobytes(), r["id"]))
    db.commit()
    log(f"  embedded {len(rows)} documents")


def similar_docs(cfg, docs):
    import numpy as np
    ids = [d["id"] for d in docs if d["embedding"]]
    if len(ids) < 2:
        return {}
    mat = np.vstack([np.frombuffer(d["embedding"], dtype="float32") for d in docs if d["embedding"]])
    sims = mat @ mat.T
    out = {}
    for i, did in enumerate(ids):
        order = np.argsort(-sims[i])
        out[did] = [{"id": ids[j], "score": round(float(sims[i, j]), 3)} for j in order
                    if j != i and sims[i, j] >= cfg["similar_min_score"]][:cfg["similar_docs_count"]]
    return out


# --------------------------------------------------------------------------- step 6: collapse duplicates

def short_agency(name):
    n = re.sub(r"^(U\.?S\.?\s+)?Department of (the )?", "", name or "", flags=re.I)
    return n.strip() or (name or "")


def title_key(title):
    """Boil a title down to its 'notice type': the last ';' segment, digits masked. Returns (key, readable tail)."""
    segs = [x.strip() for x in title.split(";") if x.strip()]
    tail = segs[-1] if len(segs) > 1 else title
    key = re.sub(r"\d+", "#", norm(tail))
    if len(key) < 20:                       # tail is too generic (e.g. 'Deletions'), use the whole title
        key, tail = re.sub(r"\d+", "#", norm(title)), title
    return key, tail


def find_groups(cfg, docs):
    """Find sets of near-identical notices: same source, agency and type, same notice-type title, and similar
    summaries (or near-duplicate titles AND summaries). Each set of >= group_min_size becomes ONE node."""
    import numpy as np
    buckets = collections.defaultdict(list)
    for d in docs:
        if d["embedding"]:
            buckets[(d["source"], d["agency0"], d["doc_type"])].append(d)
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for members in buckets.values():
        if len(members) < cfg["group_min_size"]:
            continue
        sims = np.vstack([np.frombuffer(d["embedding"], dtype="float32") for d in members])
        sims = sims @ sims.T
        keys = [title_key(d["title"])[0] for d in members]
        toks = [set(norm(d["title"]).split()) for d in members]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                same = keys[i] == keys[j] and sims[i, j] >= cfg["group_same_title_similarity"]
                jac = len(toks[i] & toks[j]) / max(1, len(toks[i] | toks[j]))
                near = sims[i, j] >= cfg["group_near_duplicate_similarity"] and jac >= 0.7
                if same or near:
                    parent[find(members[i]["id"])] = find(members[j]["id"])
    comps = collections.defaultdict(list)
    by_id = {d["id"]: d for d in docs}
    for did in list(parent):
        comps[find(did)].append(by_id[did])
    groups = []
    for members in comps.values():
        if len(members) >= cfg["group_min_size"]:
            members.sort(key=lambda d: d["pub_date"] or "", reverse=True)
            groups.append(members)
    groups.sort(key=lambda m: (-len(m), m[0]["id"]))
    return [{"id": f"g{i}", "members": m, "agency": m[0]["agency0"], "doc_type": m[0]["doc_type"],
             "tail": title_key(m[0]["title"])[1]} for i, m in enumerate(groups)]


def group_label(g):
    kind = "notices" if (g["doc_type"] or "") in ("notice", "other", "") else (g["doc_type"] + "s")
    desc = g.get("descriptor") or g["tail"]
    desc = desc if len(desc) <= 80 else desc[:79].rsplit(" ", 1)[0] + "…"
    return f"{len(g['members'])} similar {kind}: {desc} ({short_agency(g['agency'])})"


def merge_ents(members):
    ents, seen = {t: [] for t in ENTITY_TYPES}, {t: set() for t in ENTITY_TYPES}
    for m in members:
        for t in ENTITY_TYPES:
            for e in m["ents"][t]:
                if e["key"] not in seen[t]:
                    seen[t].add(e["key"])
                    ents[t].append(dict(e, doc=m["id"]))
    return ents


def make_units(docs, groups):
    """A unit is one node on the map: a single document, or a collapsed group of near-identical ones."""
    grouped = {m["id"]: g["id"] for g in groups for m in g["members"]}
    units = [{"id": g["id"], "members": g["members"], "ents": merge_ents(g["members"]), "group": g} for g in groups]
    units += [{"id": d["id"], "members": [d], "ents": merge_ents([d]), "group": None} for d in docs if d["id"] not in grouped]
    for u in units:
        u["pub_date"] = max((m["pub_date"] or "") for m in u["members"])
    return units


# --------------------------------------------------------------------------- step 7: link (weighted by rarity)

def build_links(cfg, units, cap, quiet=False):
    """Candidate links between units, with a weight per reason = how rare the shared thing is (IDF):
    ln(number of units / number of units that share it). Returns {(a, b): [reason, ...]}."""
    N = len(units)
    links = collections.defaultdict(list)
    index = collections.defaultdict(dict)          # (kind, key) -> {unit_id: entity}
    agency_idx = collections.defaultdict(dict)
    amount_idx = collections.defaultdict(dict)
    too_broad = []
    for u in units:
        for t in ("programs", "statutes"):
            for e in u["ents"][t]:
                if not e["boilerplate"]:
                    index[(t, e["key"])][u["id"]] = e
        for e in u["ents"]["agencies"]:
            agency_idx[e["key"]][u["id"]] = e
        for e in u["ents"]["dollar_amounts"]:
            if e["amount"] >= cfg["min_link_dollar_amount"]:
                amount_idx[e["key"]][u["id"]] = e

    def pairs(members):
        ids = sorted(members)
        return [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]

    def side(role, e):
        return {"role": role, "quote": e["quote"], "doc": e["doc"]}

    for (t, key), members in index.items():
        if len(members) < 2:
            continue
        if len(members) > cap:
            too_broad.append((t, next(iter(members.values()))["label"], len(members)))
            continue
        kind = "program" if t == "programs" else "statute"
        w = round(math.log(N / len(members)), 2)
        for a, b in pairs(members):
            links[(a, b)].append({"kind": kind, "label": members[a]["label"], "w": w,
                                  "a": [side(kind, members[a])], "b": [side(kind, members[b])]})
    for amt, amembers in amount_idx.items():
        if len(amembers) < 2 or len(amembers) > cap:
            continue
        w = round(math.log(N / len(amembers)), 2)
        for a, b in pairs(amembers):
            for k in [k for k, m in agency_idx.items() if a in m and b in m][:3]:
                ea, eb = agency_idx[k][a], agency_idx[k][b]
                links[(a, b)].append({
                    "kind": "agency+amount", "label": f"{ea['label']} + {amembers[a]['label']}", "w": w,
                    "a": [side("agency", ea), side("amount", amembers[a])],
                    "b": [side("agency", eb), side("amount", amembers[b])]})
    if not quiet:
        for t, label, n in too_broad:
            log(f"  note: {t[:-1]} '{label}' is shared by {n} nodes (> {cap}); too generic, not used for links")
    return links


def link_weight(reasons):
    """Strongest reason counts fully, the next half as much, and so on, so ten weak reasons don't beat one rare one."""
    ws = sorted((r["w"] for r in reasons), reverse=True)
    return round(sum(w * 0.5 ** i for i, w in enumerate(ws)), 2)


def weigh_links(cfg, raw):
    out = {}
    for pair, reasons in raw.items():
        w = link_weight(reasons)
        if w >= cfg["min_link_weight"]:
            out[pair] = {"w": w, "reasons": sorted(reasons, key=lambda r: -r["w"])}
    return out


# --------------------------------------------------------------------------- step 8: cluster + describe

def build_clusters(cfg, units_by_id, links):
    """Louvain community detection over units, edge weight = rarity-based link weight."""
    import networkx as nx
    graph = nx.Graph()
    for (a, b), l in links.items():
        graph.add_edge(a, b, weight=l["w"])
    communities = nx.community.louvain_communities(
        graph, weight="weight", resolution=cfg.get("cluster_resolution", 1.5), seed=42) if graph else []
    today = dt.date.today()
    clusters = []
    for comm in communities:
        if len(comm) < 2:
            continue
        uids = sorted(comm)
        docs = sorted((m for u in uids for m in units_by_id[u]["members"]), key=lambda m: m["pub_date"] or "", reverse=True)
        names = collections.Counter()
        for (a, b), l in links.items():
            if a in comm and b in comm:
                for r in l["reasons"]:
                    names[r["label"]] += r["w"]
        dates = [m["pub_date"] for m in docs if m["pub_date"]]

        def age(m):
            try:
                return (today - dt.date.fromisoformat(m["pub_date"])).days
            except Exception:
                return 9999
        clusters.append({"name": names.most_common(1)[0][0] if names else "Linked documents",
                         "unit_ids": uids, "doc_ids": [m["id"] for m in docs], "size": len(docs),
                         "date_from": min(dates) if dates else None, "date_to": max(dates) if dates else None,
                         "new_this_week": sum(age(m) <= 7 for m in docs), "new_30_days": sum(age(m) <= 30 for m in docs)})
    clusters.sort(key=lambda c: (-c["new_30_days"], -c["size"], c["name"]))
    clusters = clusters[:cfg["top_clusters"]]
    for i, c in enumerate(clusters):
        c["id"] = i
    return clusters


def describe_clusters(cfg, clusters, doc_by_id, entity_df, n_docs):
    """Numbers only (no AI): top shared laws/programs/funding lines and agencies for each cluster."""
    for c in clusters:
        members = [doc_by_id[i] for i in c["doc_ids"]]
        count = collections.Counter()
        kinds = {}
        for m in members:
            for t, kind in (("programs", "program"), ("statutes", "law"), ("dollar_amounts", "funding")):
                for e in m["ents"][t]:
                    if e["boilerplate"] or (t == "dollar_amounts" and e["amount"] < cfg["min_link_dollar_amount"]):
                        continue
                    count[e["label"]] += 1
                    kinds[e["label"]] = kind
        scored = sorted(((n * math.log(n_docs / max(1, entity_df.get(l, n))), l, n) for l, n in count.items() if n >= 2),
                        reverse=True)
        c["top_shared"] = [{"label": l, "kind": kinds[l], "docs": n} for _, l, n in scored[:6]]
        ag = collections.Counter(e["label"] for m in members for e in m["ents"]["agencies"])
        c["agencies"] = [{"name": n, "n": k} for n, k in ag.most_common(5)]


def cluster_relations(cfg, clusters, links, cluster_of_unit):
    """Cluster-to-cluster links: add up the unit links that cross between two clusters."""
    acc = {}
    for (a, b), l in links.items():
        ca, cb = cluster_of_unit.get(a), cluster_of_unit.get(b)
        if ca is None or cb is None or ca == cb:
            continue
        key = (min(ca, cb), max(ca, cb))
        e = acc.setdefault(key, {"a": key[0], "b": key[1], "w": 0.0, "links": 0, "shared": collections.Counter()})
        e["w"] += l["w"]
        e["links"] += 1
        for r in l["reasons"]:
            e["shared"][r["label"]] += 1
    rel = sorted(acc.values(), key=lambda e: -e["w"])
    keep, per = [], collections.Counter()
    for e in rel:                          # strongest few neighbours per cluster, so the picture stays readable
        if per[e["a"]] < cfg["max_related_clusters"] and per[e["b"]] < cfg["max_related_clusters"]:
            keep.append(e)
            per[e["a"]] += 1
            per[e["b"]] += 1
    for e in keep:
        e["w"] = round(e["w"], 1)
        e["shared"] = [{"label": l, "n": n} for l, n in e["shared"].most_common(5)]
    return keep


# --------------------------------------------------------------------------- step 9: build-time AI text (cached)

# Phrasings that signal guessing about motive or secrecy. Plain words like "hidden ownership" or "intended use" can
# legitimately appear in what a document says, so only speculative phrasings are blocked.
SPECULATION = re.compile(r"\b(secret\w*|conspir\w*|really|behind the scenes"
                         r"|(intends?|plans?|aims?|seeks?) to (secretly|covertly|quietly|undermine)"
                         r"|hidden (plan|agenda|motive)s?)\b", re.I)
AI_SYSTEM = ("You write short, neutral descriptions of groups of US government documents. Use ONLY the facts supplied. "
             "Describe what the documents share. Never guess at motives, intent, or plans, never say or imply that "
             "anyone is coordinating, and never add facts that are not in the input. Reply with one JSON object only.")


def cluster_prompt(c, doc_by_id):
    ds = [doc_by_id[i] for i in c["doc_ids"]]
    sample = "\n".join(f"- {d['title'][:120]}: {(d['summary'] or '')[:260]}" for d in ds[:8])
    shared = "\n".join(f"- {s['label']} ({s['kind']}, in {s['docs']} of {c['size']} documents)" for s in c["top_shared"]) or "- (none)"
    ag = ", ".join(f"{a['name']} ({a['n']})" for a in c["agencies"]) or "(none)"
    return (f"A group of {c['size']} documents published {c['date_from']} to {c['date_to']}.\n"
            f"Shared laws, programs and funding lines:\n{shared}\nAgencies named (number of documents): {ag}\n"
            f"Sample documents:\n{sample}\n\n"
            'Return {"title": "...", "summary": "..."}. title: at most 8 plain-English words naming what these '
            "documents are about. summary: 2-3 plain-English sentences saying what this group of documents is and why "
            "it matters, based only on what the documents themselves state (who is affected, what is regulated or funded, "
            "amounts). Mention the main shared law or program.")


def pair_prompt(r, clusters):
    ca, cb = clusters[r["a"]], clusters[r["b"]]
    shared = "; ".join(f"{s['label']} (in {s['n']} document links)" for s in r["shared"])
    return (f"Cluster A: {ca.get('title') or ca['name']} ({ca['size']} documents). Cluster B: {cb.get('title') or cb['name']} "
            f"({cb['size']} documents).\nThe two clusters are connected by {r['links']} links through these shared "
            f"entities: {shared}.\n\n"
            'Return {"sentence": "..."}: ONE plain-English sentence saying how the two clusters relate, based only on '
            "the shared entities listed. Do not mention anything not listed.")


def group_prompt(g):
    titles = "\n".join(f"- {m['title'][:140]}" for m in g["members"][:5])
    return (f"These {len(g['members'])} notices are near-duplicates from {short_agency(g['agency'])}:\n{titles}\n\n"
            'Return {"descriptor": "..."}: a lowercase phrase of at most 8 words saying what kind of notice these are '
            "(for example: duty-free entry of scientific instruments).")


def step_ai_text(cfg, db, tasks, stats):
    """tasks: [{'key','prompt','validate'}]. Returns {key: parsed dict}. Results are cached in radar.db by a
    hash of (model, prompt), so unchanged clusters/pairs/groups cost nothing on later runs."""
    out, todo = {}, []
    for t in tasks:
        h = hashlib.sha256((cfg["model"] + "\n" + t["prompt"]).encode()).hexdigest()
        row = db.execute("SELECT text FROM ai_text WHERE hash=?", (h,)).fetchone()
        if row:
            out[t["key"]] = json.loads(row["text"])
            stats["ai_cached"] += 1
        else:
            todo.append((t, h))
    if not todo:
        return out
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log(f"  ANTHROPIC_API_KEY not set: {len(todo)} texts use plain templates instead of AI-written text")
        return out
    import anthropic
    client = anthropic.Anthropic()

    def work(item):
        t, h = item
        try:
            resp = client.messages.create(model=cfg["model"], max_tokens=700, system=AI_SYSTEM,
                                          messages=[{"role": "user", "content": t["prompt"]}])
            usage = (resp.usage.input_tokens, resp.usage.output_tokens)
            return t, h, parse_json_reply("".join(b.text for b in resp.content if b.type == "text")), usage, None
        except Exception as e:
            return t, h, None, (0, 0), e

    with ThreadPoolExecutor(cfg["llm_workers"]) as ex:
        for t, h, parsed, (tin, tout), err in ex.map(work, todo):
            stats["tokens_in"] += tin
            stats["tokens_out"] += tout
            if parsed is None or not t["validate"](parsed):
                stats["ai_rejected"] += 1
                log(f"  AI text rejected for {t['key']} ({err or 'failed checks'}); using template. Text was: {str(parsed)[:260]}")
                continue
            db.execute("INSERT OR REPLACE INTO ai_text (hash, kind, text, created) VALUES (?,?,?,?)",
                       (h, t["key"].split(":")[0], json.dumps(parsed), dt.datetime.now().isoformat(timespec="seconds")))
            db.commit()
            out[t["key"]] = parsed
            stats["ai_new"] += 1
    return out


def ok_text(field, limit):
    def check(p):
        v = p.get(field)
        return isinstance(v, str) and 5 < len(v) <= limit and not SPECULATION.search(v)
    return check


def fmt_range(a, b):
    def m(x):
        try:
            return dt.date.fromisoformat(x).strftime("%b %Y")
        except Exception:
            return x or "?"
    return m(a) if m(a) == m(b) else f"{m(a)} to {m(b)}"


def template_cluster_text(c):
    shared = ", ".join(s["label"] for s in c["top_shared"][:2]) or c["name"]
    ag = ", ".join(a["name"] for a in c["agencies"][:3])
    return (f"{c['size']} documents published {fmt_range(c['date_from'], c['date_to'])} that share references to {shared}."
            + (f" Agencies named most often: {ag}." if ag else ""))


# --------------------------------------------------------------------------- step 10: export

def step_link_cluster_export(cfg, db, stats):
    log("Step 4/6-10: normalize, collapse, link, cluster, describe, export")
    norm_ = Normalizer(cfg)
    docs = []
    for r in db.execute("SELECT * FROM documents WHERE status='done' ORDER BY pub_date DESC"):
        d = dict(r)
        d["ents"] = norm_.doc_entities(json.loads(r["entities"] or "{}"))
        names = (json.loads(r["raw"]).get("agency_names") or [])
        d["agency0"] = names[0] if names else ("GAO" if r["source"] == "gao" else "Unknown")
        docs.append(d)
    n_unknown = write_unknown_entities(cfg, norm_)
    doc_by_id = {d["id"]: d for d in docs}

    # --- before: every document its own node, links not weighted (how the site worked before this change)
    singles = [{"id": d["id"], "members": [d], "ents": merge_ents([d]), "group": None} for d in docs]
    legacy = build_links(cfg, singles, cap=cfg["legacy_max_docs_per_shared_key"], quiet=True)
    legacy_nodes = {n for pair in legacy for n in pair}

    # --- collapse near-duplicates into single nodes
    groups = find_groups(cfg, docs)
    units = make_units(docs, groups)
    units_by_id = {u["id"]: u for u in units}
    N = len(units)

    # --- link, weighted by rarity
    raw = build_links(cfg, units, cap=cfg["max_docs_per_shared_key"])
    weights = sorted(link_weight(r) for r in raw.values())
    cuts = "  ".join(f">={t}: {sum(w >= t for w in weights)}" for t in (2, 3, 4, 5, 6))
    log(f"  candidate links by weight  {cuts}")
    links = weigh_links(cfg, raw)

    # --- cluster, describe
    clusters = build_clusters(cfg, units_by_id, links)
    cluster_of_unit = {u: c["id"] for c in clusters for u in c["unit_ids"]}
    cluster_of_doc = {d: c["id"] for c in clusters for d in c["doc_ids"]}
    entity_df = collections.Counter()
    for d in docs:
        for lab in {e["label"] for t in ("programs", "statutes", "dollar_amounts") for e in d["ents"][t] if not e["boilerplate"]}:
            entity_df[lab] += 1
    describe_clusters(cfg, clusters, doc_by_id, entity_df, len(docs))
    relations = cluster_relations(cfg, clusters, links, cluster_of_unit)

    # --- AI-written text (cached); falls back to plain templates without an API key
    tasks = [{"key": f"group:{g['id']}", "prompt": group_prompt(g), "validate": ok_text("descriptor", 90)} for g in groups]
    tasks += [{"key": f"cluster:{c['id']}", "prompt": cluster_prompt(c, doc_by_id),
               "validate": lambda p: ok_text("summary", 1000)(p) and ok_text("title", 80)(p)} for c in clusters]
    ai = step_ai_text(cfg, db, tasks, stats)
    for g in groups:
        g["descriptor"] = (ai.get(f"group:{g['id']}") or {}).get("descriptor")
    titled = [dict(c, title=(ai.get(f"cluster:{c['id']}") or {}).get("title")) for c in clusters]
    pair_tasks = [{"key": f"pair:{r['a']}-{r['b']}", "prompt": pair_prompt(r, titled),
                   "validate": ok_text("sentence", 350)} for r in relations]
    ai.update(step_ai_text(cfg, db, pair_tasks, stats))
    for c in clusters:
        got = ai.get(f"cluster:{c['id']}")
        c["title"] = got["title"].strip() if got else None
        c["summary"] = got["summary"].strip() if got else template_cluster_text(c)
        c["ai"] = bool(got)
    for r in relations:
        got = ai.get(f"pair:{r['a']}-{r['b']}")
        r["sentence"] = got["sentence"].strip() if got else \
            "Both clusters cite " + ", ".join(s["label"] for s in r["shared"][:3]) + "."
        r["ai"] = bool(got)

    # --- similar docs, export
    sims = similar_docs(cfg, docs)
    group_of = {m["id"]: g["id"] for g in groups for m in g["members"]}
    out_docs = []
    for d in docs:
        lanes = json.loads(d["lanes"])
        out_docs.append({
            "id": d["id"], "source": d["source"], "title": d["title"], "url": d["url"], "pub_date": d["pub_date"],
            "lanes": lanes, "crossover": len(lanes) > 1, "doc_type": d["doc_type"], "summary": d["summary"],
            "agencies": [e["label"] for e in d["ents"]["agencies"]],
            "programs": [e["label"] for e in d["ents"]["programs"]],
            "statutes": [e["label"] for e in d["ents"]["statutes"]],
            "dollar_amounts": [e["label"] for e in d["ents"]["dollar_amounts"]],
            "boiler": [e["label"] for t in ("programs", "statutes") for e in d["ents"][t] if e["boilerplate"]],
            "group": group_of.get(d["id"]), "cluster": cluster_of_doc.get(d["id"]), "similar": sims.get(d["id"], [])})
    out_groups = []
    for g in groups:
        ms = g["members"]
        lanes = sorted({l for m in ms for l in json.loads(m["lanes"])})
        out_groups.append({"id": g["id"], "label": group_label(g), "n": len(ms), "agency": g["agency"],
                           "member_ids": [m["id"] for m in ms], "lanes": lanes, "crossover": len(lanes) > 1,
                           "date_from": min(m["pub_date"] for m in ms), "date_to": max(m["pub_date"] for m in ms),
                           "cluster": cluster_of_unit.get(g["id"])})
    kept = set(cluster_of_unit)
    out_links = [{"a": a, "b": b, "w": l["w"], "reasons": l["reasons"]} for (a, b), l in links.items() if a in kept and b in kept]
    out_clusters = [{k: v for k, v in c.items() if k != "unit_ids"} for c in clusters]
    stats_out = {"docs": len(docs), "nodes_before": len(docs), "links_before": len(legacy), "linked_nodes_before": len(legacy_nodes),
                 "groups": len(groups), "docs_in_groups": sum(len(g["members"]) for g in groups), "nodes_after": N,
                 "links_after": len(out_links), "linked_nodes_after": len(kept), "min_link_weight": cfg["min_link_weight"]}
    payload = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "documents": out_docs, "groups": out_groups,
               "links": out_links, "clusters": out_clusters, "cluster_links": relations,
               "entity_df": {k: v for k, v in entity_df.items() if v >= 2}, "stats": stats_out}
    site = ROOT / cfg["site_dir"]
    site.mkdir(exist_ok=True)
    (site / "data.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    log("  --- effect of weighting + collapsing ---")
    log(f"  BEFORE: {len(docs):>5} nodes, {len(legacy):>6} links  ({len(legacy_nodes)} nodes had a link)")
    log(f"  collapse: {len(groups)} groups swallowed {stats_out['docs_in_groups']} documents -> {N} nodes")
    log(f"  AFTER : {N:>5} nodes, {len(out_links):>6} links  ({len(kept)} nodes in the {len(clusters)} top clusters; "
        f"min link weight {cfg['min_link_weight']})")
    log(f"  {len(out_docs)} documents, {len(clusters)} clusters, {len(relations)} cluster relations -> "
        f"{cfg['site_dir']}/data.json; {n_unknown} unrecognized names logged to unknown_entities.txt")
    return len(out_docs), len(out_links), len(clusters)


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Gov Radar daily pipeline")
    ap.add_argument("--no-fetch", action="store_true", help="skip downloading; rebuild outputs from radar.db")
    ap.add_argument("--since", help="backfill: first publication date, YYYY-MM-DD (Federal Register only)")
    ap.add_argument("--until", help="backfill: last publication date, YYYY-MM-DD")
    ap.add_argument("--max-docs", type=int, help="override max_new_docs_per_run (use for backfills)")
    args = ap.parse_args()
    cfg = load_config()
    if args.since or args.until:
        cfg["_since"] = args.since or "2024-01-01"
        cfg["_until"] = args.until or dt.date.today().isoformat()
    if args.max_docs:
        cfg["max_new_docs_per_run"] = args.max_docs
    db = open_db(cfg)
    stats = collections.Counter()
    started = time.time()

    if not args.no_fetch:
        stats["docs_new"] = step_fetch(cfg, db)
    step_text(cfg, db)
    step_summarize(cfg, db, stats)
    step_embed(cfg, db)
    n_docs, n_links, n_clusters = step_link_cluster_export(cfg, db, stats)

    price = cfg["price_per_million_tokens"]
    cost = stats["tokens_in"] / 1e6 * price["input"] + stats["tokens_out"] / 1e6 * price["output"]
    pending = db.execute("SELECT COUNT(*) FROM documents WHERE status='pending'").fetchone()[0]
    log("\n=== Run summary ===")
    log(f"New documents stored this run : {stats['docs_new']}")
    log(f"Documents summarized this run : {stats['docs_summarized']}   (still pending: {pending})")
    log(f"Tokens used                   : {stats['tokens_in']:,} in / {stats['tokens_out']:,} out")
    log(f"Estimated cost                : ${cost:.4f}  ({cfg['model']})")
    log(f"AI texts (clusters/pairs/groups): {stats['ai_new']} written, {stats['ai_cached']} reused from cache, {stats['ai_rejected']} rejected")
    log(f"Site data                     : {n_docs} documents, {n_links} links, {n_clusters} clusters")
    log(f"Time                          : {time.time() - started:.0f}s")
    if stats["summary_attempted"] and not stats["docs_summarized"]:
        log("ERROR: every summary failed (see 'failed ...' lines above). Failing the run so it is not mistaken for success.")
        sys.exit(1)


if __name__ == "__main__":
    main()
