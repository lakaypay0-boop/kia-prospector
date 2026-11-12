#!/usr/bin/env python3
"""Kreyòl IA — module de prospection (GitHub Actions).

Collecte des prospects (OpenStreetMap + recherche web Tavily + extraction LLM)
et les envoie à l'agent Cloudflare (POST /api/prospects). Rien n'est envoyé
aux prospects : l'agent prépare des brouillons que tu valides sur Telegram.

Bibliothèque standard uniquement (aucune dépendance à installer).
"""
import argparse, datetime, json, os, random, re, sys, time
import urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
HAITI_BBOX = "17.9,-74.6,20.2,-71.5"  # sud, ouest, nord, est
UA = "kreyolia-prospector/1.0 (contact: kreyolia.com)"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http_json(url, data=None, headers=None, timeout=60, method=None):
    body = None
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode()
            h.setdefault("Content-Type", "application/json")
        else:
            body = data if isinstance(data, bytes) else str(data).encode()
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------- téléphones
def normalize_phone(raw, default_cc="509"):
    """Retourne un numéro international '+XXXXXXXX' ou '' si invalide.
    Haïti : 8 chiffres locaux -> +509XXXXXXXX."""
    if not raw:
        return ""
    first = re.split(r"[;/,]| ou | or ", str(raw))[0]
    digits = re.sub(r"\D", "", first)
    if first.strip().startswith("+") or digits.startswith("00"):
        if digits.startswith("00"):
            digits = digits[2:]
        return "+" + digits if 9 <= len(digits) <= 15 else ""
    if default_cc == "33":
        if len(digits) == 10 and digits.startswith("0"):
            return "+33" + digits[1:]
        if len(digits) == 11 and digits.startswith("33"):
            return "+" + digits
        return ""
    if len(digits) == 8:
        return "+" + default_cc + digits
    if digits.startswith(default_cc) and len(digits) == 11:
        return "+" + digits
    if len(digits) == 10 and default_cc != "1":  # numéro nord-américain
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return ""


TOLL_FREE = ("800", "833", "844", "855", "866", "877", "888")


def is_toll_free(norm):
    """Numéros verts nord-américains : pas de WhatsApp possible, aucun intérêt pour nous."""
    return norm.startswith("+1") and len(norm) == 12 and norm[2:5] in TOLL_FREE


def phone_channel(norm):
    """Haïti : mobiles en 3/4 ; France : mobiles en 6/7 ; le reste = fixe."""
    if norm.startswith("+509") and len(norm) == 12 and norm[4] not in "34":
        return "téléphone"
    if norm.startswith("+33") and len(norm) == 12 and norm[3] not in "67":
        return "téléphone"
    return "whatsapp"


def country_cc(country):
    """Indicatif par défaut selon le pays du prospect (France : numéros en 0X XX XX XX XX)."""
    c = (country or "").strip().lower()
    return "33" if c in ("france", "fr") else "509"


# ---------------------------------------------------------------- OpenStreetMap
def build_overpass_query(filters, bbox=HAITI_BBOX, limit=300):
    parts = []
    for f in filters:
        k, v = f.split("=", 1)
        parts.append(f'nwr["{k}"="{v}"]["name"]({bbox});')
    return "[out:json][timeout:20];(" + "".join(parts) + f");out center {limit};"


def fetch_osm(filters, budget=45):
    """Une requête légère par filtre ; s'arrête dès qu'un filtre répond ou que le budget (s) est épuisé."""
    start, last, elements = time.time(), None, []
    for f in filters:
        q = build_overpass_query([f], limit=150)
        got = False
        for url in OVERPASS:
            if time.time() - start > budget:
                break
            try:
                r = http_json(url, data="data=" + urllib.parse.quote(q),
                              headers={"Content-Type": "application/x-www-form-urlencoded"},
                              timeout=20)
                elements += r.get("elements", [])
                got = True
                break
            except Exception as e:  # noqa
                last = e
                log("overpass échec", url, repr(e))
        if got and len(elements) >= 40:
            break
    if not elements and last is not None:
        raise RuntimeError(f"Overpass indisponible: {last}")
    return {"elements": elements}


def parse_osm(data, seg):
    """Garde les entités avec un nom ET au moins un moyen de contact."""
    out = []
    for el in data.get("elements", []):
        t = el.get("tags") or {}
        name = (t.get("name") or "").strip()
        if not name:
            continue
        phone = normalize_phone(t.get("contact:mobile") or t.get("contact:phone") or t.get("phone") or t.get("mobile"))
        email = (t.get("contact:email") or t.get("email") or "").strip().split(";")[0]
        website = (t.get("contact:website") or t.get("website") or "").strip()
        wa = normalize_phone(t.get("contact:whatsapp"))
        if not (phone or email or wa):
            continue
        if email and EMAIL_RE.fullmatch(email):
            contact, channel = email, "email"
        else:
            contact = wa or phone
            channel = "whatsapp" if wa else phone_channel(phone)
        score = 60
        if not website:
            score += 15  # pas de site : meilleure cible
        if wa:
            score += 10
        city = t.get("addr:city") or t.get("addr:suburb") or ""
        notes = f"{seg['label']}" + (f" à {city}" if city else "")
        notes += "; sans site web" if not website else f"; site actuel: {website}"
        out.append({
            "name": name, "segment": seg["label"], "country": seg["country"],
            "channel": channel, "contact": contact,
            "source_url": f"https://www.openstreetmap.org/{el.get('type','node')}/{el.get('id','')}",
            "score": min(score, 95), "notes": notes,
        })
    return out


# ---------------------------------------------------------------- web + LLM
def tavily_search(query, key, n=10):
    r = http_json("https://api.tavily.com/search", data={
        "api_key": key, "query": query, "max_results": n,
        "search_depth": "basic", "include_answer": False}, timeout=60)
    return r.get("results", [])


EXTRACT_SYS = (
    "Tu extrais des prospects B2B depuis des résultats de recherche web. "
    "Réponds UNIQUEMENT par un tableau JSON. Chaque élément : "
    '{"name":str,"contact":str,"channel":"email|whatsapp|web","country":str,"notes":str,"source_url":str}. '
    "N'invente JAMAIS un email ou un numéro : le contact doit apparaître mot pour mot dans le texte fourni. "
    "Vise des ORGANISATIONS (cliniques, hôtels, écoles, commerces, restaurants, associations, ONG, entreprises) : "
    "ignore les praticiens isolés, les particuliers, les annuaires eux-mêmes, les grandes multinationales, "
    "les agences des Nations Unies et grandes organisations internationales (UNICEF, PAM, UNESCO...), "
    "les gouvernements, ministères, ambassades, missions diplomatiques, services publics et lignes d'assistance, "
    "les banques et grandes institutions financières, "
    "les résultats sans contact, et TOUTE entreprise de services numériques "
    "(agence web, agence digitale, développement de sites ou d'applications, informatique, marketing digital, freelance IT). "
    "Préfère un email professionnel (pas msn/hotmail/gmail) "
    "ou un numéro de téléphone/WhatsApp pour l'organisation. "
    'Écris "notes" en FRANÇAIS : une courte phrase (type d\'organisation, ville) ; pas de numéro de téléphone dans les notes. '
    'Écris "country" en français, au nom complet : États-Unis, Canada, France, Haïti, etc. '
    "Si rien d'exploitable, réponds []."
)


def extract_json_array(text):
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        v = json.loads(m.group(0))
    except Exception:
        return []
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


RETRY_CODES = (429, 500, 502, 503, 504)


def call_with_retry(fn, tries=3, base=8):
    """Réessaie après 429/5xx/timeout, avec attente croissante (8 s, 16 s...)."""
    for i in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_CODES or i == tries - 1:
                raise
            wait = base * (2 ** i)
            ra = e.headers.get("Retry-After") if e.headers else None
            if ra and ra.isdigit():
                wait = min(max(wait, int(ra)), 60)
            log(f"HTTP {e.code}, nouvel essai dans {wait}s")
            time.sleep(wait)
        except (TimeoutError, urllib.error.URLError) as e:
            if i == tries - 1:
                raise
            log(f"réseau/timeout ({e!r}), nouvel essai dans {base}s")
            time.sleep(base)


def _openai_compat(url, key, model, system, user, extra=None):
    payload = {"model": model,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
               "max_tokens": 3000, "temperature": 0.1}
    payload.update(extra or {})
    r = call_with_retry(lambda: http_json(
        url, data=payload, headers={"Authorization": "Bearer " + key}, timeout=60))
    return r["choices"][0]["message"].get("content") or ""


def llm_complete(system, user, env=os.environ):
    """Chaîne de repli : Groq -> Gemini -> Mistral. Première réponse non vide gagne."""
    providers = []
    if env.get("GROQ_API_KEY"):
        providers.append(("groq", lambda: _openai_compat(
            "https://api.groq.com/openai/v1/chat/completions", env["GROQ_API_KEY"],
            env.get("GROQ_MODEL", "openai/gpt-oss-120b"), system, user,
            {"reasoning_effort": "low"})))
    if env.get("GEMINI_API_KEY"):
        model = env.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

        def gem():
            r = call_with_retry(lambda: http_json(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                data={"systemInstruction": {"parts": [{"text": system}]},
                      "contents": [{"role": "user", "parts": [{"text": user}]}],
                      "generationConfig": {"temperature": 0.1, "maxOutputTokens": 3000}},
                headers={"x-goog-api-key": env["GEMINI_API_KEY"]}, timeout=60))
            parts = r["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts if not p.get("thought"))
        providers.append(("gemini", gem))
    if env.get("MISTRAL_API_KEY"):
        providers.append(("mistral", lambda: _openai_compat(
            "https://api.mistral.ai/v1/chat/completions", env["MISTRAL_API_KEY"],
            env.get("MISTRAL_MODEL", "mistral-small-latest"), system, user)))
    for name, fn in providers:
        try:
            txt = fn()
            if txt and txt.strip():
                return txt
            log(name, "réponse vide")
        except Exception as e:  # noqa
            log(f"{name} échec:", e)
    return ""


def verify_candidate(c, corpus):
    """Garde-fou anti-hallucination : le contact doit figurer dans le texte source."""
    contact = (c.get("contact") or "").strip()
    name = (c.get("name") or "").strip()
    if not name or not contact:
        return None
    low = corpus.lower()
    if "@" in contact:
        if not EMAIL_RE.fullmatch(contact) or contact.lower() not in low:
            return None
        c["channel"] = "email"
    else:
        digits = re.sub(r"\D", "", contact)
        if len(digits) < 8 or digits[-8:] not in re.sub(r"\D", "", corpus):
            return None
        norm = normalize_phone(contact, country_cc(c.get("country")))
        if not norm or is_toll_free(norm):
            return None
        contact = norm
        c["channel"] = phone_channel(norm)
    c["contact"] = contact
    return c


import unicodedata


def _norm(x):
    x = unicodedata.normalize("NFD", str(x or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9@. ]+", " ", x)


EXCLUDE_RE = re.compile(
    r"\b(banque|bank|banco|societe financiere|nations unies|united nations|unicef|unops|unesco|"
    r"ambassade|embassy|consulat|consulate|ministere|ministry|gouvernement|government|oea|oas|"
    r"mission diplomatique|police|sauvetage)\b|@[a-z0-9.-]*(\.gov|\.gouv\.ht|\.un\.org|diplomatie\.ht)\b")


def excluded(c, seg):
    """Filtre côté code (le LLM ignore parfois la consigne) : pays hors zone, banques, ONU, État."""
    allowed = seg.get("countries")
    if allowed and _norm(c.get("country")).strip() not in {_norm(a).strip() for a in allowed}:
        return True
    blob = _norm(" ".join(str(c.get(k) or "") for k in ("name", "notes", "contact")))
    return bool(EXCLUDE_RE.search(blob))


def fetch_web(seg, tavily_key, rng, max_queries=3):
    out = []
    queries = list(seg["queries"])
    rng.shuffle(queries)
    for n, q in enumerate(queries[:max_queries]):
        if n:
            time.sleep(10)  # évite le 429 des paliers gratuits
        try:
            res = tavily_search(q, tavily_key)
        except Exception as e:  # noqa
            log("tavily échec:", q, e)
            continue
        corpus = "\n\n".join(
            f"URL: {r.get('url','')}\nTITRE: {r.get('title','')}\n{(r.get('content') or '')[:1200]}"
            for r in res)
        if not corpus.strip():
            continue
        system = EXTRACT_SYS + ((" " + seg["focus"]) if seg.get("focus") else "")
        cands = extract_json_array(llm_complete(system, corpus))
        for c in cands:
            c = verify_candidate(c, corpus)
            if not c or excluded(c, seg):
                continue
            out.append({
                "name": c["name"][:200], "segment": seg["label"],
                "country": (c.get("country") or seg["country"])[:100],
                "channel": c["channel"], "contact": c["contact"],
                "source_url": (c.get("source_url") or "")[:500],
                "score": 70, "notes": (c.get("notes") or seg["label"])[:500],
            })
    return out


# ---------------------------------------------------------------- envoi
def _rank(p):
    return {"email": 3, "whatsapp": 2}.get(p.get("channel"), 1)


def dedupe(items):
    """Un seul contact par organisation : email > WhatsApp > téléphone fixe."""
    owners = {}
    for p in items:
        owners.setdefault(p["contact"].lower(), set()).add(re.sub(r"\W+", " ", p["name"].lower()).strip())
    items = [p for p in items if len(owners[p["contact"].lower()]) == 1]
    best = {}
    for p in items:
        k = re.sub(r"\W+", " ", p["name"].lower()).strip()
        if k not in best or _rank(p) > _rank(best[k]):
            best[k] = p
    return list(best.values())


def push(prospects, base_url, token, batch=50):
    added = 0
    for i in range(0, len(prospects), batch):
        chunk = prospects[i:i + batch]
        r = http_json(base_url.rstrip("/") + "/api/prospects",
                      data={"prospects": chunk},
                      headers={"Authorization": "Bearer " + token}, timeout=60)
        added += int(r.get("added", 0))
        log(f"envoyé {len(chunk)} -> ajoutés {r.get('added')}")
    return added


def pick_segment(segments, forced=None, today=None):
    if forced:
        for s in segments:
            if s["id"] == forced:
                return s
        raise SystemExit(f"segment inconnu: {forced}")
    today = today or datetime.date.today()
    return segments[today.timetuple().tm_yday % len(segments)]


def run(args, env=os.environ):
    with open(os.path.join(HERE, "segments.json"), encoding="utf-8") as f:
        segments = json.load(f)
    seg = pick_segment(segments, args.segment)
    rng = random.Random()
    log("segment:", seg["id"])
    items = []
    def web(max_queries=2):
        if not env.get("TAVILY_API_KEY"):
            log("TAVILY_API_KEY absent : pas de repli web.")
            return []
        return fetch_web(seg, env["TAVILY_API_KEY"], rng, max_queries)

    if seg["kind"] == "osm":
        filters = list(seg["osm"])
        rng.shuffle(filters)
        try:
            items = parse_osm(fetch_osm(filters), seg)
            rng.shuffle(items)
        except Exception as e:  # noqa
            log("OSM indisponible, repli sur la recherche web :", repr(e))
            items = web(5)
            if not items:
                raise
    else:
        items = web(3)
    items = dedupe(items)[: args.max]
    log(f"{len(items)} prospects retenus")
    if args.dry_run or not items:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0
    base, tok = env.get("KIA_AGENT_URL"), env.get("KIA_API_TOKEN")
    if not base or not tok:
        raise SystemExit("KIA_AGENT_URL / KIA_API_TOKEN manquants")
    added = push(items, base, tok)
    log(f"terminé : {added} nouveaux prospects")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--segment", default=os.environ.get("SEGMENT") or None)
    ap.add_argument("--max", type=int, default=25)
    ap.add_argument("--dry-run", action="store_true")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    import urllib.parse  # noqa
    sys.exit(main())
