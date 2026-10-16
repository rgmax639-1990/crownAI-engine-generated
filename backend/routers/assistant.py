"""AI assistant (EN / HI / Hinglish) answering ONLY from published CMS content, with citations.

Guard rails:
  * never asks for or accepts OTP / PIN / CVV / password: such requests get a safety reply;
  * PII is redacted before anything is stored (Aadhaar, card, PAN, mobile, email, account numbers);
  * retrieval is restricted to published product pages, FAQs, notices, policies and home sections,
    so every answer is traceable to a published content version (cited by version id);
  * when nothing relevant is found, or the user asks for a person, a human hand-off is offered.
"""
from __future__ import annotations

import difflib
import re
import sqlite3
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from audit import audit
from core import api_error, forbidden, invalid, jdump, jload, new_id, now_iso, one, redact_pii
from db import get_db
from security import AuthContext, auth_optional

router = APIRouter(tags=["assistant"])

LOCALES = ("en", "hi", "hinglish")
SOURCE_TYPES = ("product_page", "faq", "notice", "policy", "home_section")
MAX_MESSAGES = 200

# --------------------------------------------------------------------------- guard rails
# Latin-script words use word boundaries ("pin" must not match "shopping"); Devanagari is matched as is.
_SECRET_RE = re.compile(
    r"\b(?:otp|o\.t\.p|one[\s-]?time[\s-]?password|m-?pin|upi[\s-]?pin|atm[\s-]?pin|pin(?!\s*-?\s*code)|cvv|cvc|"
    r"password|passcode)\b|ओटीपी|पिन(?!\s*कोड)|पासवर्ड|सीवीवी|गुप्त\s*कोड",
    re.IGNORECASE,
)
_SHARE_RE = re.compile(
    r"\b(?:share|sharing|shared|send|sending|sent|give|giving|provide|disclose|ask(?:ed|ing|s)?|caller|called|whatsapp|"
    r"forward|tell\s+(?:you|u)|bata(?:o|un|ya|na|du|doon|dun)?|de\s*d(?:u|oon|o)|dedo|bhej(?:o|na|un|du|a)?|"
    r"maang(?:a|e|na|raha|rahe)?|mang(?:a|e|na|raha|rahe)?)\b|बता|भेज|माँग|मांग|शेयर|साझा|दे\s*दूँ|दे\s*दो",
    re.IGNORECASE,
)
_DIGITS_SECRET_RE = re.compile(
    r"\b(?:otp|cvv|password|pin(?!\s*-?\s*code))\b\D{0,12}\d{3,8}|(?:ओटीपी|पिन(?!\s*कोड))\D{0,12}\d{3,8}",
    re.IGNORECASE,
)
_HUMAN_RE = re.compile(
    r"\b(human|agent|person|executive|representative|customer\s*care|call\s*centre|call\s*center|talk\s*to\s*(?:someone|a\s*person)|"
    r"insaan|insan|aadmi|kisi\s*se\s*baat|baat\s*karni|baat\s*karna)\b|एजेंट|व्यक्ति|इंसान|किसी\s*से\s*बात|ग्राहक\s*सेवा",
    re.IGNORECASE,
)
_MOBILE_RE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_LONG_NUMBER_RE = re.compile(r"(?<!\d)\d{9,18}(?!\d)")  # account numbers and similar


def redact_chat(text: str) -> str:
    """Mask every kind of PII we can recognise before a message is stored or echoed."""
    t = redact_pii(text or "")
    t = _EMAIL_RE.sub("[email removed]", t)
    t = _MOBILE_RE.sub("[mobile removed]", t)
    t = _LONG_NUMBER_RE.sub(lambda m: "XXXX" + m.group(0)[-4:], t)
    t = _DIGITS_SECRET_RE.sub("[secret removed]", t)
    return t


def is_secret_request(text: str) -> bool:
    """True when the message involves sharing an OTP / PIN / CVV / password (any direction)."""
    if _DIGITS_SECRET_RE.search(text):
        return True
    return bool(_SECRET_RE.search(text)) and bool(_SHARE_RE.search(text))


# --------------------------------------------------------------------------- retrieval
STOPWORDS = {
    "the", "a", "an", "is", "are", "of", "to", "for", "in", "on", "and", "or", "what", "how", "can", "i", "me", "my", "you",
    "your", "do", "does", "with", "about", "please", "tell", "want", "need", "know", "much", "which", "this", "that", "it",
    "be", "get", "kya", "hai", "hain", "ka", "ki", "ke", "ko", "mein", "me", "se", "kaise", "kitna", "kitni", "mujhe", "batao",
    "bataye", "chahiye", "aur", "ya", "kar", "karna", "karo", "hota", "hoti", "kab", "kahan", "kaun", "kyun",
    "क्या", "है", "हैं", "का", "की", "के", "को", "में", "से", "कैसे", "कितना", "कितनी", "मुझे", "बताओ", "बताएं", "चाहिए", "और",
    "या", "कर", "करना", "होता", "होती", "कब", "कहाँ", "कौन", "पर", "एक", "यह", "वह",
}

# Hinglish / Hindi -> English synonyms (and the reverse is added below), so a Hinglish question can
# match English or Hindi content.
TRANSLIT: dict[str, list[str]] = {
    "byaj": ["interest", "rate", "ब्याज"], "byaaj": ["interest", "rate", "ब्याज"], "ब्याज": ["interest", "rate"],
    "dar": ["rate"], "दर": ["rate"], "dare": ["rates"], "दरें": ["rates", "rate"],
    "khata": ["account", "खाता"], "khaata": ["account", "खाता"], "खाता": ["account"], "khate": ["accounts", "account"],
    "bachat": ["savings", "बचत"], "बचत": ["savings"],
    "jama": ["deposit", "जमा"], "जमा": ["deposit", "fd"], "sawadhi": ["fixed", "fd"], "सावधि": ["fixed", "fd"],
    "fd": ["fixed", "deposit", "एफडी"], "एफडी": ["fd", "fixed", "deposit"], "rd": ["recurring", "deposit"],
    "karz": ["loan", "ऋण"], "karj": ["loan", "ऋण"], "rin": ["loan", "ऋण"], "ऋण": ["loan"], "lone": ["loan"], "लोन": ["loan"],
    "ghar": ["home", "housing"], "घर": ["home"], "gaadi": ["vehicle", "car"], "gadi": ["vehicle", "car"], "गाड़ी": ["vehicle", "car"],
    "vyaktigat": ["personal"], "व्यक्तिगत": ["personal"], "kist": ["emi", "instalment"], "किस्त": ["emi"], "emi": ["instalment", "ईएमआई"],
    "card": ["कार्ड"], "कार्ड": ["card"], "credit": ["क्रेडिट"], "debit": ["डेबिट"],
    "shikayat": ["complaint", "grievance", "शिकायत"], "शिकायत": ["complaint", "grievance"],
    "dhokha": ["fraud"], "dhokhadhadi": ["fraud"], "धोखाधड़ी": ["fraud"], "dhokhadhari": ["fraud"], "thagi": ["fraud", "scam"],
    "block": ["ब्लॉक"], "band": ["block", "close"], "बंद": ["block", "close"],
    "shulk": ["charges", "fee"], "शुल्क": ["charges", "fee"], "charge": ["charges", "fee"], "fees": ["charges", "fee"],
    "shakha": ["branch"], "शाखा": ["branch"], "atm": ["एटीएम"], "एटीएम": ["atm"],
    "paisa": ["money", "amount"], "rupaye": ["amount"], "suraksha": ["safe", "security"], "सुरक्षा": ["safe", "security"],
    "beema": ["insurance", "dicgc"], "bima": ["insurance", "dicgc"], "बीमा": ["insurance", "dicgc"],
    "varisht": ["senior"], "वरिष्ठ": ["senior"], "nri": ["एनआरआई"], "एनआरआई": ["nri"],
    "kholna": ["open"], "kholo": ["open"], "खोलना": ["open"], "खोलें": ["open"], "suchna": ["notice"], "सूचना": ["notice"],
    "niti": ["policy"], "नीति": ["policy"], "gopniyata": ["privacy"], "गोपनीयता": ["privacy"],
}

TOKEN_RE = re.compile(r"[a-z0-9]+|[ऀ-ॿ]+", re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text or "")]


def _expand(tokens: list[str]) -> set[str]:
    out = set()
    for t in tokens:
        if t in STOPWORDS or len(t) < 2:
            continue
        out.add(t)
        for syn in TRANSLIT.get(t, []):
            out.add(syn.lower())
        if t.endswith("s") and len(t) > 3:
            out.add(t[:-1])
    return out


def _flatten(value: Any, out: list[str], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(value, str):
        s = value.strip()
        if s and not s.startswith(("http://", "https://", "/")) and len(s) > 1:
            out.append(s)
    elif isinstance(value, dict):
        for k, v in value.items():
            if str(k).endswith(("_id", "href", "url", "slug", "icon", "image")):
                continue
            _flatten(v, out, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _flatten(v, out, depth + 1)


def _title(body: dict, slug: str) -> str:
    for key in ("title", "name", "heading", "question", "product_name"):
        v = body.get(key) if isinstance(body, dict) else None
        if isinstance(v, str) and v.strip():
            return v.strip()
    return slug.replace("-", " ").title()


def _sources(conn: sqlite3.Connection, locales: tuple[str, ...]) -> list[dict]:
    rows = conn.execute(
        f"""SELECT i.content_type, i.slug, i.locale, v.id AS version_id, v.body FROM content_items i
            JOIN content_versions v ON v.id = i.published_version_id
            WHERE i.published_version_id IS NOT NULL AND i.content_type IN ({','.join('?' * len(SOURCE_TYPES))})
              AND i.locale IN ({','.join('?' * len(locales))})""",
        (*SOURCE_TYPES, *locales),
    ).fetchall()
    out = []
    for r in rows:
        body = jload(r["body"], {}) or {}
        parts: list[str] = []
        _flatten(body, parts)
        title = _title(body if isinstance(body, dict) else {}, r["slug"])
        out.append({"content_type": r["content_type"], "slug": r["slug"], "locale": r["locale"], "version_id": r["version_id"],
                    "title": title, "text": " \n".join(parts), "parts": parts})
    return out


def _score(query: set[str], doc: dict) -> float:
    if not query:
        return 0.0
    title_tokens = set(_tokens(doc["title"])) | set(_tokens(doc["slug"].replace("-", " ")))
    text_tokens = _tokens(doc["text"])
    counts: dict[str, int] = {}
    for t in text_tokens:
        counts[t] = counts.get(t, 0) + 1
    score = 0.0
    for q in query:
        if q in title_tokens:
            score += 4
        elif any(len(q) >= 4 and difflib.SequenceMatcher(None, q, tt).ratio() >= 0.84 for tt in title_tokens):
            score += 2.5  # fuzzy: typos and transliteration variants ("intrest", "acount")
        c = counts.get(q, 0)
        if c:
            score += min(3, c) * 0.8
        elif len(q) >= 5 and difflib.get_close_matches(q, list(counts.keys())[:400], n=1, cutoff=0.86):
            score += 0.6
    if doc["content_type"] == "faq":
        score *= 1.1
    return score


def _extract(query: set[str], doc: dict, limit: int = 320) -> str:
    sentences: list[str] = []
    for p in doc["parts"]:
        sentences.extend(s.strip() for s in re.split(r"(?<=[.!?।])\s+", p) if len(s.strip()) > 15)
    if not sentences:
        return doc["title"]
    best = max(sentences, key=lambda s: (len(query & set(_tokens(s))), -abs(len(s) - 160)))
    if len(best) > limit:
        best = best[: limit - 1].rsplit(" ", 1)[0] + "…"
    return best


def _href(doc: dict, locale: str) -> str:
    loc = "hi" if locale == "hi" else "en"
    if doc["content_type"] == "product_page":
        return f"/{loc}/products/{doc['slug']}"
    if doc["content_type"] == "home_section":
        return f"/{loc}"
    return f"/{loc}/pages/{doc['slug']}"


# --------------------------------------------------------------------------- copy
COPY = {
    "en": {
        "greeting": "Hello! I can help with our accounts, deposits, loans, cards, rates and services. I answer only from the "
                    "bank's published information.",
        "disclaimer": "The bank never asks for your OTP, PIN, CVV or password. Never share them with anyone, including this assistant.",
        "safety": "For your safety I cannot help with sharing an OTP, PIN, CVV or password. The bank NEVER asks for them — not by "
                  "call, SMS, email or chat. If someone asked you for one, it is likely fraud: do not share it, call 1930 or "
                  "report fraud on our website now.",
        "nothing": "I could not find this in the bank's published information. Would you like to talk to a person?",
        "human": "Sure, I can connect you to a customer service officer.",
        "found": "Here is what our published information says:",
        "quick": ["FD interest rates", "Open a savings account", "Block my card", "File a complaint", "Talk to a human"],
        "handoff": "You are in the queue to talk to a customer service officer. Position: {pos}. Typical wait: about {mins} minutes.",
    },
    "hi": {
        "greeting": "नमस्ते! मैं खातों, जमा, ऋण, कार्ड, ब्याज दरों और सेवाओं में आपकी मदद कर सकता हूँ। मैं केवल बैंक की प्रकाशित "
                    "जानकारी से उत्तर देता हूँ।",
        "disclaimer": "बैंक कभी भी आपका OTP, PIN, CVV या पासवर्ड नहीं माँगता। इन्हें किसी से भी साझा न करें, इस सहायक से भी नहीं।",
        "safety": "आपकी सुरक्षा के लिए मैं OTP, PIN, CVV या पासवर्ड साझा करने में मदद नहीं कर सकता। बैंक इन्हें कभी नहीं माँगता — "
                  "न कॉल, न SMS, न ईमेल, न चैट पर। अगर किसी ने माँगा है तो यह धोखाधड़ी हो सकती है: साझा न करें, तुरंत 1930 पर "
                  "कॉल करें या हमारी वेबसाइट पर धोखाधड़ी रिपोर्ट करें।",
        "nothing": "यह जानकारी बैंक की प्रकाशित सामग्री में नहीं मिली। क्या आप किसी अधिकारी से बात करना चाहेंगे?",
        "human": "ज़रूर, मैं आपको ग्राहक सेवा अधिकारी से जोड़ सकता हूँ।",
        "found": "हमारी प्रकाशित जानकारी के अनुसार:",
        "quick": ["एफडी ब्याज दरें", "बचत खाता खोलें", "कार्ड ब्लॉक करें", "शिकायत दर्ज करें", "अधिकारी से बात करें"],
        "handoff": "आप ग्राहक सेवा अधिकारी से बात करने की कतार में हैं। आपका स्थान: {pos}। अनुमानित प्रतीक्षा: लगभग {mins} मिनट।",
    },
    "hinglish": {
        "greeting": "Namaste! Main accounts, deposits, loans, cards, rates aur services mein madad kar sakta hoon. Main sirf bank "
                    "ki published jaankari se jawab deta hoon.",
        "disclaimer": "Bank kabhi bhi aapka OTP, PIN, CVV ya password nahi maangta. Inhe kisi ke saath share na karein.",
        "safety": "Aapki suraksha ke liye main OTP, PIN, CVV ya password share karne mein madad nahi kar sakta. Bank inhe kabhi "
                  "nahi maangta — na call, na SMS, na chat par. Agar kisi ne maanga hai to yeh fraud ho sakta hai: share na "
                  "karein, turant 1930 par call karein ya website par fraud report karein.",
        "nothing": "Yeh jaankari bank ki published content mein nahi mili. Kya aap kisi officer se baat karna chahenge?",
        "human": "Zaroor, main aapko customer service officer se connect kar sakta hoon.",
        "found": "Hamari published jaankari ke anusaar:",
        "quick": ["FD byaj dar", "Savings khata kholna", "Card block karna", "Shikayat darj karna", "Insaan se baat karein"],
        "handoff": "Aap customer service officer se baat karne ki queue mein hain. Position: {pos}. Andaazan wait: lagbhag {mins} minute.",
    },
}


# --------------------------------------------------------------------------- endpoints
class SessionIn(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    locale: str = "en"


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = ""
    locale: Optional[str] = None


def _session(conn: sqlite3.Connection, session_id: str, ctx: AuthContext | None) -> dict:
    if not session_id or len(session_id) > 64:
        raise api_error(404, "not_found", "Chat session not found. Start a new chat.")
    s = one(conn, "SELECT * FROM assistant_sessions WHERE id = ?", (session_id,))
    if not s:
        raise api_error(404, "not_found", "Chat session not found. Start a new chat.")
    if s["user_id"] and (ctx is None or ctx.kind != "user" or ctx.user_id != s["user_id"]):
        raise forbidden("This chat belongs to a signed-in customer. Sign in to continue it or start a new chat.")
    return s


@router.post("/assistant/sessions", status_code=201)
def start_session(body: SessionIn, request: Request, ctx: AuthContext | None = Depends(auth_optional),
                  conn: sqlite3.Connection = Depends(get_db)):
    if body.locale not in LOCALES:
        raise invalid("locale", "Choose en, hi or hinglish")
    sid = new_id()
    now = now_iso()
    user_id = ctx.user_id if ctx and ctx.kind == "user" else None
    copy = COPY[body.locale]
    greeting = {"id": new_id(), "role": "assistant", "text": copy["greeting"], "at": now, "citations": [], "cited_version_ids": []}
    conn.execute(
        """INSERT INTO assistant_sessions (id, user_id, locale, messages, handoff_status, created_at, updated_at)
           VALUES (?,?,?,?, 'none', ?, ?)""",
        (sid, user_id, body.locale, jdump([greeting]), now, now),
    )
    return {"session_id": sid, "locale": body.locale, "authenticated": bool(user_id), "greeting": copy["greeting"],
            "disclaimer": copy["disclaimer"], "quick_replies": copy["quick"], "messages": [greeting], "handoff_status": "none"}


@router.post("/assistant/sessions/{session_id}/messages")
def post_message(session_id: str, body: MessageIn, request: Request, ctx: AuthContext | None = Depends(auth_optional),
                 conn: sqlite3.Connection = Depends(get_db)):
    s = _session(conn, session_id, ctx)
    raw = (body.text or "").strip()
    if not raw:
        raise invalid("text", "Type a question")
    if len(raw) > 500:
        raise invalid("text", "Keep your question within 500 characters")
    locale = body.locale if body.locale in LOCALES else s["locale"]
    copy = COPY[locale]
    stored_text = redact_chat(raw)  # raw text is never stored
    now = now_iso()
    user_msg = {"id": new_id(), "role": "user", "text": stored_text, "at": now}

    citations: list[dict] = []
    safety = False
    handoff = False
    if is_secret_request(raw):
        safety = True
        answer = copy["safety"]
        quick = ["Report fraud", "Block my card", "Talk to a human"] if locale != "hi" else ["धोखाधड़ी रिपोर्ट करें", "कार्ड ब्लॉक करें", "अधिकारी से बात करें"]
    elif _HUMAN_RE.search(raw):
        handoff = True
        answer = copy["human"]
        quick = copy["quick"][:4]
    else:
        query = _expand(_tokens(redact_chat(raw)))
        content_locales = ("hi", "en") if locale in ("hi", "hinglish") else ("en",)
        docs = _sources(conn, content_locales)
        ranked = sorted(((round(_score(query, d), 3), d) for d in docs), key=lambda x: -x[0])
        top = [d for sc, d in ranked if sc >= 3.0][:3]
        if not top:
            handoff = True
            answer = copy["nothing"]
            quick = copy["quick"]
        else:
            # Prefer content in the reader's language for the extract (Hinglish reads English best).
            pref = "hi" if locale == "hi" else "en"
            top.sort(key=lambda d: 0 if d["locale"] == pref else 1)
            seen = set()
            lines = [copy["found"]]
            for d in top:
                if d["slug"] in seen:
                    continue
                seen.add(d["slug"])
                lines.append(f"• {d['title']}: {_extract(query, d)}")
                citations.append({"title": d["title"], "href": _href(d, locale), "version_id": d["version_id"],
                                  "content_type": d["content_type"]})
                if len(citations) >= 2:
                    break
            answer = "\n".join(lines)
            quick = copy["quick"]

    bot_msg = {"id": new_id(), "role": "assistant", "text": answer, "at": now_iso(), "citations": citations,
               "cited_version_ids": [c["version_id"] for c in citations], "handoff_suggested": handoff, "safety": safety}
    messages = (jload(s["messages"], []) or []) + [user_msg, bot_msg]
    conn.execute("UPDATE assistant_sessions SET messages = ?, updated_at = ? WHERE id = ?",
                 (jdump(messages[-MAX_MESSAGES:]), now_iso(), s["id"]))
    if safety:
        audit(conn, "admin", "assistant.safety_refusal", actor_type="customer" if s["user_id"] else "visitor",
              actor_id=s["user_id"], entity_type="assistant_session", entity_id=s["id"], details={"locale": locale})
    return {
        "session_id": s["id"],
        "user_message": user_msg,
        "message": bot_msg,
        "reply": answer,
        "citations": citations,
        "handoff_suggested": handoff,
        "safety": safety,
        "quick_replies": quick,
        "redacted": stored_text != raw,
    }


@router.post("/assistant/sessions/{session_id}/handoff")
def request_handoff(session_id: str, ctx: AuthContext | None = Depends(auth_optional), conn: sqlite3.Connection = Depends(get_db)):
    s = _session(conn, session_id, ctx)
    now = now_iso()
    if s["handoff_status"] != "requested":
        conn.execute("UPDATE assistant_sessions SET handoff_status = 'requested', updated_at = ? WHERE id = ?", (now, s["id"]))
        s = one(conn, "SELECT * FROM assistant_sessions WHERE id = ?", (s["id"],)) or s
    # Position = requested sessions that asked before (or at the same time as) this one.
    pos = conn.execute(
        """SELECT COUNT(*) FROM assistant_sessions WHERE handoff_status = 'requested'
           AND (updated_at < ? OR (updated_at = ? AND id <= ?))""",
        (s["updated_at"], s["updated_at"], s["id"]),
    ).fetchone()[0]
    pos = max(1, int(pos))
    conn.execute("UPDATE assistant_sessions SET queue_position = ? WHERE id = ?", (pos, s["id"]))
    copy = COPY[s["locale"]]
    mins = pos * 3
    text = copy["handoff"].format(pos=pos, mins=mins)
    msg = {"id": new_id(), "role": "assistant", "text": text, "at": now_iso(), "citations": [], "cited_version_ids": [],
           "system": True}
    messages = (jload(s["messages"], []) or []) + [msg]
    conn.execute("UPDATE assistant_sessions SET messages = ? WHERE id = ?", (jdump(messages[-MAX_MESSAGES:]), s["id"]))
    return {"session_id": s["id"], "handoff_status": "requested", "queue_position": pos, "estimated_wait_minutes": mins,
            "message": msg, "text": text}
