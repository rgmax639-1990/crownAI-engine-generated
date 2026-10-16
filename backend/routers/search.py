"""Site search: products, FAQs, branches/ATMs (incl. exact IFSC) and notices/policies.

Query and documents go through the same normaliser, which maps Devanagari and Hinglish
banking terms onto English canonical tokens (एफडी -> fd, byaj/ब्याज -> interest, ...), so a
Hindi or Hinglish query matches English content and vice versa. Unknown tokens are
corrected against the index vocabulary (difflib), which also drives ``did_you_mean``.
"""
from __future__ import annotations

import difflib
import re
import sqlite3
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Response

from content_service import PAGE_TYPES, publish_due, published_items, query_locale
from core import jload, many
from db import get_db

router = APIRouter(tags=["search"])

IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
PIN_RE = re.compile(r"^[1-9][0-9]{5}$")
TOKEN_RE = re.compile(r"[0-9a-zऀ-ॿ]+")

# Multi-word phrases first (applied to lower-cased text before tokenising).
PHRASES = [
    ("fixed deposits", "fd"), ("fixed deposit", "fd"), ("term deposit", "fd"), ("recurring deposit", "rd"),
    ("housing loan", "home loan"), ("mortgage loan", "home loan"), ("savings bank", "savings"), ("saving account", "savings account"),
    ("सावधि जमा", "fd"), ("फिक्स्ड डिपॉजिट", "fd"), ("आवर्ती जमा", "rd"), ("बचत खाता", "savings account"),
    ("गृह ऋण", "home loan"), ("होम लोन", "home loan"), ("पर्सनल लोन", "personal loan"), ("व्यक्तिगत ऋण", "personal loan"),
    ("क्रेडिट कार्ड", "credit card"), ("ब्याज दर", "interest rate"), ("ब्याज दरें", "interest rate"),
    ("fixed dipozit", "fd"), ("ghar ka loan", "home loan"), ("byaj dar", "interest rate"),
]

TERM_MAP = {
    # Devanagari
    "एफडी": "fd", "एफ़डी": "fd", "आरडी": "rd", "ब्याज": "interest", "दर": "rate", "दरें": "rate", "दरों": "rate",
    "ऋण": "loan", "लोन": "loan", "कर्ज": "loan", "कर्ज़": "loan", "बचत": "savings", "खाता": "account", "खाते": "account",
    "शाखा": "branch", "शाखाएँ": "branch", "ब्रांच": "branch", "एटीएम": "atm", "कार्ड": "card", "क्रेडिट": "credit",
    "डेबिट": "debit", "घर": "home", "आवास": "home", "होम": "home", "गृह": "home", "व्यक्तिगत": "personal", "पर्सनल": "personal",
    "जमा": "deposit", "सावधि": "fixed", "आवर्ती": "recurring", "वरिष्ठ": "senior", "बुजुर्ग": "senior",
    "शिकायत": "grievance", "धोखाधड़ी": "fraud", "धोखा": "fraud", "फ्रॉड": "fraud", "नीति": "policy", "गोपनीयता": "privacy",
    "वेतन": "salary", "सैलरी": "salary", "चालू": "current", "करंट": "current", "म्यूचुअल": "mutual", "फंड": "fund",
    "निवेश": "investment", "एनआरआई": "nri", "प्रवासी": "nri", "शुल्क": "charges", "चार्ज": "charges", "प्रभार": "charges",
    "ईएमआई": "emi", "किस्त": "emi", "कैलकुलेटर": "calculator", "बीमा": "insurance", "डीआईसीजीसी": "dicgc", "ब्लॉक": "block",
    "आईएफएससी": "ifsc", "न्यूनतम": "minimum", "शेष": "balance", "बैलेंस": "balance", "ऑनलाइन": "online", "खोलें": "open",
    "खोलना": "open", "वाहन": "vehicle", "गाड़ी": "vehicle", "कार": "vehicle", "एमएसएमई": "msme", "व्यवसाय": "business",
    "कर": "tax", "टीडीएस": "tds", "समय": "time", "सुरक्षित": "safe", "बैंकिंग": "banking", "पात्रता": "eligibility",
    "दस्तावेज़": "documents", "दस्तावेज": "documents",
    # Hinglish / common spellings
    "byaj": "interest", "byaaj": "interest", "vyaj": "interest", "biyaj": "interest", "dar": "rate", "rates": "rate",
    "loans": "loan", "karz": "loan", "karza": "loan", "karj": "loan", "bachat": "savings", "saving": "savings",
    "khata": "account", "khaata": "account", "khaate": "account", "accounts": "account", "shakha": "branch",
    "branches": "branch", "atms": "atm", "cards": "card", "ghar": "home", "housing": "home", "jama": "deposit",
    "deposits": "deposit", "shikayat": "grievance", "complaint": "grievance", "complaints": "grievance",
    "dhokha": "fraud", "dhokhadhadi": "fraud", "tankhwah": "salary", "naukri": "salary", "nivesh": "investment",
    "charge": "charges", "fees": "charges", "fee": "charges", "kist": "emi", "kisht": "emi", "emis": "emi",
    "gaadi": "vehicle", "gadi": "vehicle", "car": "vehicle", "auto": "vehicle", "vyapar": "business",
    "fds": "fd", "rds": "rd", "mf": "mutual fund", "sip": "mutual fund", "citizens": "citizen",
    "kholna": "open", "kholo": "open", "kaise": "how",
}

STOPWORDS = {
    "the", "a", "an", "of", "for", "in", "on", "to", "and", "is", "are", "what", "how", "my", "me", "i", "with", "do", "can",
    "your", "you", "it", "at", "by", "or", "be", "bank", "example",
    "का", "की", "के", "में", "है", "हैं", "क्या", "कैसे", "से", "पर", "और", "को", "मेरा", "मेरी", "मेरे", "एक", "यह", "बैंक",
    "kya", "ka", "ki", "ke", "hai", "me", "mein", "se", "par", "aur", "ko", "mera", "meri",
}

GROUP_LABELS = {
    "en": {"products": "Products", "faqs": "FAQs", "branches": "Branches & ATMs", "notices": "Notices & policies"},
    "hi": {"products": "उत्पाद", "faqs": "सामान्य प्रश्न", "branches": "शाखाएँ और एटीएम", "notices": "सूचनाएँ और नीतियाँ"},
}


# ------------------------------------------------------------------ normalisation
def canonical_tokens(text: str) -> list[str]:
    s = f" {(text or '').lower()} "
    for src, dst in PHRASES:
        s = s.replace(src, f" {dst} ")
    out: list[str] = []
    for tok in TOKEN_RE.findall(s):
        mapped = TERM_MAP.get(tok, tok)
        for t in mapped.split():
            if t not in STOPWORDS and (len(t) > 1 or t.isdigit()):
                out.append(t)
    return out


def _flat_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flat_text(v) for k, v in value.items() if k not in ("document_ids", "apply_href", "calculator_href"))
    if isinstance(value, list):
        return " ".join(_flat_text(v) for v in value)
    return ""


class Doc:
    __slots__ = ("group", "id", "title", "snippet", "href", "meta", "title_toks", "kw_toks", "body_toks", "title_lc", "exact")

    def __init__(self, group: str, id: str, title: str, snippet: str, href: str, meta: dict, keywords: str, body: str):
        self.group, self.id, self.title, self.snippet, self.href, self.meta = group, id, title or "", snippet or "", href, meta
        self.title_toks = set(canonical_tokens(self.title))
        self.kw_toks = set(canonical_tokens(keywords))
        self.body_toks = set(canonical_tokens(body))
        self.title_lc = self.title.lower()
        self.exact: set[str] = set()


def _snippet(text: str, n: int = 180) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def build_index(conn: sqlite3.Connection, locale: str) -> list[Doc]:
    docs: list[Doc] = []
    for r in published_items(conn, "product_page", locale):
        b = r["body"]
        hero = b.get("hero") or {}
        kw = " ".join([b.get("category") or "", b.get("product_code") or "", r["slug"].replace("-", " "),
                       " ".join(b.get("keywords") or [])])
        docs.append(Doc("products", r["item_id"], hero.get("title") or r["slug"], hero.get("subtitle") or "",
                        f"/{locale}/products/{r['slug']}", {"category": b.get("category"), "is_fallback": r["is_fallback"],
                                                            "rate_label": hero.get("rate_label")}, kw, _flat_text(b)))
    for r in published_items(conn, "faq", locale):
        b = r["body"]
        href = (f"/{locale}/products/{b['product_slug']}#faqs" if b.get("product_slug")
                else f"/{locale}/search?q={quote(b.get('question') or '')}")
        docs.append(Doc("faqs", r["item_id"], b.get("question") or "", _snippet(b.get("answer") or ""), href,
                        {"answer": b.get("answer"), "category": b.get("category"), "is_fallback": r["is_fallback"]},
                        " ".join([b.get("category") or "", " ".join(b.get("keywords") or [])]), b.get("answer") or ""))
    for ct in PAGE_TYPES:
        for r in published_items(conn, ct, locale):
            b = r["body"]
            docs.append(Doc("notices", r["item_id"], b.get("title") or r["slug"], _snippet(b.get("summary") or ""),
                            f"/{locale}/pages/{r['slug']}", {"content_type": ct, "date": b.get("date"), "is_fallback": r["is_fallback"]},
                            r["slug"].replace("-", " ") + " " + ct, _flat_text(b)))
    for br in many(conn, "SELECT * FROM branches"):
        title = (br.get("name_hi") or br["name"]) if locale == "hi" else br["name"]
        services = " ".join(jload(br.get("services"), []) or [])
        kw = " ".join(str(x) for x in (br["kind"], br["kind"].replace("_", " "), br["city"], br["state"], br["pin_code"],
                                       br.get("ifsc") or "", br["name"], br.get("name_hi") or "", services,
                                       "24x7" if br.get("is_24x7") else "", "locker" if br.get("locker") else "",
                                       "forex" if br.get("forex") else ""))
        d = Doc("branches", br["id"], title, f"{br['address']}, {br['city']} {br['pin_code']}", f"/{locale}/branches/{br['slug']}",
                {"kind": br["kind"], "ifsc": br.get("ifsc"), "city": br["city"], "pin_code": br["pin_code"],
                 "address": br["address"], "phone": br.get("phone")}, kw, br["address"])
        if br.get("ifsc"):
            d.exact.add(br["ifsc"].upper())
        d.exact.add(br["pin_code"])
        docs.append(d)
    return docs


def _vocab(docs: list[Doc]) -> set[str]:
    v: set[str] = set()
    for d in docs:
        v |= d.title_toks | d.kw_toks | d.body_toks
    return v


def _correct(tokens: list[str], vocab: set[str]) -> tuple[list[tuple[str, float]], dict[str, str]]:
    """Return (token, weight) pairs after typo correction, plus {original: corrected} for did-you-mean."""
    latin_terms = [k for k in TERM_MAP if k.isascii()]
    out: list[tuple[str, float]] = []
    fixes: dict[str, str] = {}
    vocab_list = sorted(vocab)
    for t in tokens:
        if t in vocab or t.isdigit() or len(t) < 3:
            out.append((t, 1.0))
            continue
        cand = difflib.get_close_matches(t, vocab_list, n=1, cutoff=0.75)
        if not cand and t.isascii():
            term = difflib.get_close_matches(t, latin_terms, n=1, cutoff=0.8)
            if term:
                mapped = TERM_MAP[term[0]].split()
                fixes[t] = term[0]
                out.extend((m, 0.8) for m in mapped)
                continue
        if cand:
            fixes[t] = cand[0]
            out.append((cand[0], 0.8))
        else:
            out.append((t, 1.0))
    return out, fixes


def _score(d: Doc, terms: list[tuple[str, float]], q_lc: str) -> float:
    if not terms:
        return 0.0
    score, matched = 0.0, 0
    for t, w in terms:
        s = 0.0
        if t in d.title_toks:
            s = 6
        elif t in d.kw_toks:
            s = 4
        elif t in d.body_toks:
            s = 1.5
        elif len(t) >= 3 and any(x.startswith(t) for x in d.title_toks):
            s = 3
        elif len(t) >= 3 and any(x.startswith(t) for x in d.kw_toks | d.body_toks):
            s = 0.8
        if s:
            matched += 1
            score += s * w
    if not matched:
        return 0.0
    coverage = matched / len(terms)
    score += 4 * coverage + (3 if matched == len(terms) else 0)
    if len(q_lc) >= 3 and q_lc in d.title_lc:
        score += 5
    if coverage < 0.5 and len(terms) >= 3:
        score *= 0.5
    return round(score, 3)


def run_search(conn: sqlite3.Connection, q: str, locale: str, limit: int) -> dict:
    docs = build_index(conn, locale)
    raw = (q or "").strip()
    q_upper = re.sub(r"\s+", "", raw).upper()
    tokens = canonical_tokens(raw)
    terms, fixes = _correct(tokens, _vocab(docs))
    q_lc = raw.lower()
    words_upper = {w.upper() for w in re.split(r"[\s,]+", raw) if w}
    ifsc_codes = {w for w in words_upper | {q_upper} if IFSC_RE.match(w)}
    exact_ifsc = bool(ifsc_codes)
    scored: list[tuple[float, Doc]] = []
    for d in docs:
        s = _score(d, terms, q_lc)
        if d.group == "branches" and q_upper:
            if d.exact & ifsc_codes:
                s += 1000
            elif d.exact & (words_upper | {q_upper}):
                s += 80
            elif len(q_upper) >= 4 and d.meta.get("ifsc") and str(d.meta["ifsc"]).startswith(q_upper):
                s += 50
        if s > 0:
            scored.append((s, d))
    scored.sort(key=lambda x: (-x[0], x[1].title))
    labels = GROUP_LABELS[locale]
    groups: dict[str, dict] = {}
    for s, d in scored:
        g = groups.setdefault(d.group, {"key": d.group, "label": labels[d.group], "total": 0, "top_score": s, "results": []})
        g["total"] += 1
        if len(g["results"]) < limit:
            g["results"].append({"type": d.group, "id": d.id, "title": d.title, "snippet": d.snippet, "href": d.href,
                                 "score": s, "meta": d.meta})
    ordered = sorted(groups.values(), key=lambda g: -g["top_score"])
    did_you_mean = None
    if fixes:
        words = raw.split()
        rebuilt = [fixes.get(w.lower(), w) for w in words]
        candidate = " ".join(rebuilt)
        if candidate.lower() != raw.lower():
            did_you_mean = candidate
    return {
        "query": raw,
        "locale": locale,
        "normalized_tokens": [t for t, _ in terms],
        "did_you_mean": did_you_mean,
        "exact_ifsc_match": exact_ifsc and any(d.group == "branches" and d.exact & ifsc_codes for _, d in scored),
        "total": sum(g["total"] for g in ordered),
        "groups": ordered,
    }


@router.get("/public/search")
def public_search(response: Response, q: str = Query("", max_length=200), locale: Optional[str] = Query(None),
                  limit: int = Query(5, ge=1, le=20), conn: sqlite3.Connection = Depends(get_db)):
    loc = query_locale(locale)
    publish_due(conn)
    response.headers["Cache-Control"] = "public, max-age=30"
    if not q.strip():
        return {"query": "", "locale": loc, "normalized_tokens": [], "did_you_mean": None, "exact_ifsc_match": False,
                "total": 0, "groups": []}
    return run_search(conn, q, loc, limit)


@router.get("/public/search/suggest")
def public_suggest(response: Response, q: str = Query("", max_length=100), locale: Optional[str] = Query(None),
                   limit: int = Query(8, ge=1, le=15), conn: sqlite3.Connection = Depends(get_db)):
    """Type-ahead: titles whose words start with what was typed (any script), best first."""
    loc = query_locale(locale)
    response.headers["Cache-Control"] = "public, max-age=30"
    raw = q.strip()
    if len(raw) < 2:
        return {"query": raw, "suggestions": []}
    docs = build_index(conn, loc)
    raw_lc = raw.lower()
    toks = canonical_tokens(raw)
    last = toks[-1] if toks else raw_lc
    head = toks[:-1]
    q_upper = raw.replace(" ", "").upper()
    out: list[tuple[float, Doc]] = []
    for d in docs:
        s = 0.0
        if d.title_lc.startswith(raw_lc):
            s += 10
        elif f" {raw_lc}" in f" {d.title_lc}":
            s += 6
        all_toks = d.title_toks | d.kw_toks
        if last and any(x.startswith(last) for x in all_toks):
            s += 3 + sum(1 for h in head if h in all_toks)
        if d.group == "branches" and d.meta.get("ifsc") and len(q_upper) >= 4 and str(d.meta["ifsc"]).startswith(q_upper):
            s += 8
        if s:
            s += {"products": 2, "faqs": 1, "notices": 0.5, "branches": 0}[d.group]
            out.append((s, d))
    out.sort(key=lambda x: (-x[0], len(x[1].title)))
    seen: set[str] = set()
    suggestions = []
    for _, d in out:
        if d.title in seen:
            continue
        seen.add(d.title)
        suggestions.append({"label": d.title, "type": d.group, "href": d.href,
                            "hint": d.meta.get("ifsc") if d.group == "branches" else d.meta.get("category")})
        if len(suggestions) >= limit:
            break
    return {"query": raw, "suggestions": suggestions}
