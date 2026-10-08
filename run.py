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
import html
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
    for i in range(4):
        try:
            r = requests.get(url, headers=UA, timeout=45, **kw)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def fr_body_text(raw_text_url):
    """Plain body text of a Federal Register document, without the GPO header block."""
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

    with ThreadPoolExecutor(4) as ex:
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
    return f"TITLE: {row['title']}\nABSTRACT: {raw.get('abstract') or '(none)'}\nBODY: {body[:cfg['body_chars']]}"


def build_gao_text(cfg, row):
    raw = json.loads(row["raw"])
    desc = strip_html(raw.get("description"))
    return f"TITLE: {row['title']}\nABSTRACT/SUMMARY: {desc[:cfg['body_chars'] + 1500]}"


def step_text(cfg, db):
    log("Step 2: get text")
    rows = db.execute("SELECT * FROM documents WHERE text IS NULL").fetchall()

    def work(row):
        return row["id"], (build_fr_text if row["source"] == "federal_register" else build_gao_text)(cfg, row)

    with ThreadPoolExecutor(4) as ex:
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


# --------------------------------------------------------------------------- step 6: link

def build_links(cfg, docs):
    """docs: list of dicts with 'id' and normalized 'ents'. Returns {(a,b): [reason, ...]}."""
    links = collections.defaultdict(list)
    index = collections.defaultdict(dict)          # (kind, key) -> {doc_id: entity}
    agency_idx = collections.defaultdict(dict)     # agency key -> {doc_id: entity}
    amount_idx = collections.defaultdict(dict)     # amount -> {doc_id: entity}
    too_broad = []
    for d in docs:
        for t in ("programs", "statutes"):
            for e in d["ents"][t]:
                if not e["boilerplate"]:
                    index[(t, e["key"])][d["id"]] = e
        for e in d["ents"]["agencies"]:
            agency_idx[e["key"]][d["id"]] = e
        for e in d["ents"]["dollar_amounts"]:
            if e["amount"] >= cfg["min_link_dollar_amount"]:
                amount_idx[e["key"]][d["id"]] = e

    def pairs(members):
        ids = sorted(members)
        return [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]

    for (t, key), members in index.items():
        if len(members) < 2:
            continue
        if len(members) > cfg["max_docs_per_shared_key"]:
            too_broad.append((t, next(iter(members.values()))["label"], len(members)))
            continue
        kind = "program" if t == "programs" else "statute"
        for a, b in pairs(members):
            links[(a, b)].append({"kind": kind, "label": members[a]["label"],
                                  "a": [{"role": kind, "quote": members[a]["quote"]}],
                                  "b": [{"role": kind, "quote": members[b]["quote"]}]})
    # agency AND dollar amount
    for amt, amembers in amount_idx.items():
        if len(amembers) < 2:
            continue
        for a, b in pairs(amembers):
            shared = [k for k, m in agency_idx.items() if a in m and b in m]
            for k in shared[:3]:
                ea, eb = agency_idx[k][a], agency_idx[k][b]
                links[(a, b)].append({
                    "kind": "agency+amount", "label": f"{ea['label']} + {amembers[a]['label']}",
                    "a": [{"role": "agency", "quote": ea["quote"]}, {"role": "amount", "quote": amembers[a]["quote"]}],
                    "b": [{"role": "agency", "quote": eb["quote"]}, {"role": "amount", "quote": amembers[b]["quote"]}]})
    for t, label, n in too_broad:
        log(f"  note: {t[:-1]} '{label}' is shared by {n} documents (> {cfg['max_docs_per_shared_key']}); too generic, not used for links")
    return links


# --------------------------------------------------------------------------- step 7: cluster

def build_clusters(cfg, doc_by_id, links):
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in links:
        parent[find(a)] = find(b)
    groups = collections.defaultdict(list)
    for x in list(parent):
        groups[find(x)].append(x)
    today = dt.date.today()

    def days_old(did):
        try:
            return (today - dt.date.fromisoformat(doc_by_id[did]["pub_date"])).days
        except Exception:
            return 9999

    clusters = []
    for members in groups.values():
        mset = set(members)
        names = collections.Counter()
        fallback = collections.Counter()
        for (a, b), reasons in links.items():
            if a in mset:
                for r in reasons:
                    (fallback if r["kind"] == "agency+amount" else names)[r["label"]] += 1
        counter = names or fallback
        name = counter.most_common(1)[0][0] if counter else "Linked documents"
        members.sort(key=lambda d: doc_by_id[d]["pub_date"] or "", reverse=True)
        clusters.append({"name": name, "size": len(members), "doc_ids": members,
                         "new_this_week": sum(days_old(d) <= 7 for d in members),
                         "new_30_days": sum(days_old(d) <= 30 for d in members)})
    clusters.sort(key=lambda c: (-c["new_30_days"], -c["size"], c["name"]))
    clusters = clusters[:cfg["top_clusters"]]
    for i, c in enumerate(clusters):
        c["id"] = i
    return clusters


# --------------------------------------------------------------------------- step 8: export

def step_link_cluster_export(cfg, db):
    log("Step 4/6/7/8: normalize, link, cluster, export")
    norm_ = Normalizer(cfg)
    rows = db.execute("SELECT * FROM documents WHERE status='done' ORDER BY pub_date DESC").fetchall()
    docs = []
    for r in rows:
        d = dict(r)
        d["ents"] = norm_.doc_entities(json.loads(r["entities"] or "{}"))
        docs.append(d)
    n_unknown = write_unknown_entities(cfg, norm_)
    doc_by_id = {d["id"]: d for d in docs}
    links = build_links(cfg, docs)
    clusters = build_clusters(cfg, doc_by_id, links)
    cluster_of = {did: c["id"] for c in clusters for did in c["doc_ids"]}
    sims = similar_docs(cfg, docs)

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
            "cluster": cluster_of.get(d["id"]), "similar": sims.get(d["id"], [])})
    kept = set(cluster_of)
    out_links = [{"a": a, "b": b, "reasons": rs} for (a, b), rs in links.items() if a in kept and b in kept]
    payload = {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
               "documents": out_docs, "links": out_links, "clusters": clusters}
    site = ROOT / cfg["site_dir"]
    site.mkdir(exist_ok=True)
    (site / "data.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"  {len(out_docs)} documents, {len(out_links)} links, {len(clusters)} clusters -> {cfg['site_dir']}/data.json; "
        f"{n_unknown} unrecognized names logged to unknown_entities.txt")
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
    n_docs, n_links, n_clusters = step_link_cluster_export(cfg, db)

    price = cfg["price_per_million_tokens"]
    cost = stats["tokens_in"] / 1e6 * price["input"] + stats["tokens_out"] / 1e6 * price["output"]
    pending = db.execute("SELECT COUNT(*) FROM documents WHERE status='pending'").fetchone()[0]
    log("\n=== Run summary ===")
    log(f"New documents stored this run : {stats['docs_new']}")
    log(f"Documents summarized this run : {stats['docs_summarized']}   (still pending: {pending})")
    log(f"Tokens used                   : {stats['tokens_in']:,} in / {stats['tokens_out']:,} out")
    log(f"Estimated cost                : ${cost:.4f}  ({cfg['model']})")
    log(f"Site data                     : {n_docs} documents, {n_links} links, {n_clusters} clusters")
    log(f"Time                          : {time.time() - started:.0f}s")
    if stats["summary_attempted"] and not stats["docs_summarized"]:
        log("ERROR: every summary failed (see 'failed ...' lines above). Failing the run so it is not mistaken for success.")
        sys.exit(1)


if __name__ == "__main__":
    main()
