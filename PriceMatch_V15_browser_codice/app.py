from flask import Flask, render_template, request, jsonify, redirect, session
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin, urlencode, quote_plus, unquote_plus
from xml.etree import ElementTree
from db import get_connection, get_match_precedenti, get_ricerca_precedente, get_ricerche_recenti, salva_match, salva_prodotto, salva_ricerca
import requests, re, json, os, concurrent.futures, sys, subprocess, threading, time
import html
import ipaddress
import logging
import secrets
import socket
import tempfile
import unicodedata
from datetime import datetime, timezone
from functools import lru_cache, wraps
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_jwt_extended import JWTManager, create_access_token
from werkzeug.security import generate_password_hash, check_password_hash
import uuid

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
    JWT_SECRET_KEY=os.environ.get("JWT_SECRET_KEY", os.environ.get("SECRET_KEY", "dev-only-change-me")),
    SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "sqlite:///database.db"),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)
jobs = {}
saved_searches = {}
_jobs_lock = threading.Lock()
_active_searches = {}
search_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=max(1, int(os.environ.get("SEARCH_WORKERS", "3"))),
    thread_name_prefix="pricematch",
)

@app.route('/')
def home():
    if not session.get('user_id'):
        return redirect('/login')
    return render_template('home.html', dashboard=build_dashboard(session['user_id']))

db = SQLAlchemy(app)
migrate = Migrate(app, db)
jwt = JWTManager(app)

#tabella degli utenti
class User(db.Model):
    __tablename__ = 'utenti'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class SearchHistory(db.Model):
    __tablename__ = 'storico_ricerche_locale'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('utenti.id', ondelete='CASCADE'), nullable=False)
    code = db.Column(db.String(100), nullable=False)
    result = db.Column(db.JSON, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (db.UniqueConstraint('user_id', 'code', name='uq_storico_utente_codice'),)


with app.app_context():
    db.create_all()


def _csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": _csrf_token()}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Autenticazione richiesta"}), 401
            return redirect("/login")
        return view(*args, **kwargs)
    return wrapped


def csrf_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        expected = session.get("csrf_token")
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            return jsonify({"error": "Token CSRF non valido"}), 403
        return view(*args, **kwargs)
    return wrapped

#creazione utente
@app.route('/register', methods=['GET', 'POST'])
def register():
    #se aperto da browser, mostra il modulo di registrazione
    if request.method == 'GET':
        return '''
        <div style="font-family: sans-serif; padding: 40px; text-align: center;">
            <h2>Registrazione PriceMatch</h2>
            <form action="/register" method="POST" style="display: inline-block; text-align: left;">
                <p><input type="email" name="email" placeholder="Email" required style="width: 250px; padding: 8px;"></p>
                <p><input type="password" name="password" placeholder="Password" required style="width: 250px; padding: 8px;"></p>
                <button type="submit" style="width: 100%; padding: 10px; background: #28a745; color: white; border: none; cursor: pointer;">Registrati</button>
            </form>
            <p><a href="/login">Hai già un account? Accedi qui</a></p>
        </div>
        '''

    #gestione dati inviati (sia Form HTML che JSON)
    data = request.get_json(silent=True) or request.form

    email = clean(data.get('email')).lower()
    password = data.get('password')

    if not email or not password:
        return jsonify({"error": "Email e password obbligatorie"}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Email già registrata"}), 400

    new_user = User(email=email)
    new_user.set_password(password)
    db.session.add(new_user)
    db.session.commit()

    return jsonify({"message": "Utente registrato con successo!"}), 201

#aggiunge il login
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return '''
        <div style="font-family: sans-serif; padding: 40px; text-align: center;">
            <h2>Login PriceMatch</h2>
            <form action="/login" method="POST" style="display: inline-block; text-align: left;">
                <p><input type="email" name="email" placeholder="Email" required style="width: 250px; padding: 8px;"></p>
                <p><input type="password" name="password" placeholder="Password" required style="width: 250px; padding: 8px;"></p>
                <button type="submit" style="width: 100%; padding: 10px; background: #0056b3; color: white; border: none; cursor: pointer;">Accedi</button>
            </form>
            <p style="margin-top: 15px;"><a href="/register">Non hai un account? Registrati qui</a></p>
        </div>
        '''

    data = request.form if request.form else (request.get_json(silent=True) or {})
    email = clean(data.get('email')).lower()
    password = data.get('password') or ""
    user = User.query.filter_by(email=email).first()

    if user and user.check_password(password):
        session["user_id"] = user.id
        _csrf_token()
        if request.is_json:
            return jsonify({
                "token": create_access_token(identity=str(user.id)),
                "csrf_token": session["csrf_token"],
            })
        return redirect("/")

    if request.is_json:
        return jsonify({"error": "Credenziali non valide"}), 401
    return "Credenziali non valide", 401


@app.route('/logout', methods=['POST'])
@login_required
@csrf_required
def logout():
    session.clear()
    return redirect('/login')

@app.route('/compagnie', methods=['GET', 'POST'])
@login_required
def compagnie():
    message = None
    error = None
    if request.method == 'POST':
        expected = session.get('csrf_token')
        supplied = request.form.get('csrf_token')
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            return jsonify({"error": "Token CSRF non valido"}), 403
        name = clean(request.form.get('name'))
        url = clean(request.form.get('url'))
        if not name or not url:
            error = "Inserisci sia il nome sia l'indirizzo del sito."
        elif len(name) > 120 or len(url) > 500:
            error = "Nome o indirizzo troppo lungo."
        elif not is_safe_public_url(url):
            error = "Inserisci un URL pubblico valido che inizi con http:// o https://."
        else:
            normalized = normalize_base(url)
            companies = load_sites()
            duplicate = any(
                company['name'].lower() == name.lower()
                or domain(company['url']) == domain(normalized)
                for company in companies
            )
            if duplicate:
                error = "Questa compagnia è già presente."
            else:
                companies.append({"name": name, "url": normalized})
                try:
                    with _cache_lock:
                        safe_save_json(os.path.join(ROOT, 'sites.json'), companies)
                    message = "Compagnia aggiunta correttamente."
                except OSError:
                    logger.exception("Salvataggio compagnia non riuscito")
                    error = "Non è stato possibile salvare la compagnia."
    return render_template('compagnie.html', companies=load_sites(), message=message, error=error)

@app.route('/monitorati')
@login_required
def monitorati():
    return render_template('monitorati.html')

@app.route('/personalizzati')
@login_required
def personalizzati():
    return render_template('personalizzati.html')

@app.route('/statistiche-dettaglio')
@login_required
def statistiche_dettaglio():
    return "<h1>Statistiche Ingrandite</h1><p>Grafici a schermo intero.</p>"


ROOT = os.path.dirname(__file__)
CACHE_FILE = os.path.join(ROOT, "product_cache.json")
HEADERS = {
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151.0 Safari/537.36",
    "Accept-Language":"it-IT,it;q=0.9,en;q=0.8"
}
TIMEOUT = 7
BROWSER_TIMEOUT = 16000
_cache_lock = threading.Lock()
SITE_TIMEOUT = 60
MAX_INTERNAL_RESULTS = 5
MAX_BING_RESULTS = 5
_install_lock = threading.Lock()
_install_attempted = False

def clean(x): return re.sub(r"\s+"," ",str(x or "")).strip()
def norm_code(x): return re.sub(r"[^A-Z0-9]","",str(x or "").upper())

def _search_worker(task_id, query, user_id):
    logger.info("Avvio ricerca %s per il codice %s", task_id, query)
    try:
        summary = avvia_scraping_siti(query)
        result = {
            "status": "completed",
            "code": query,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            **summary,
        }
        with _jobs_lock:
            jobs[task_id] = {**result, "user_id": user_id}
            saved_searches[(user_id, query.upper())] = result
        try:
            with app.app_context():
                history = SearchHistory.query.filter_by(user_id=user_id, code=query.upper()).first()
                if history is None:
                    history = SearchHistory(user_id=user_id, code=query.upper(), result=result)
                    db.session.add(history)
                else:
                    history.result = result
                    history.updated_at = datetime.now(timezone.utc)
                db.session.commit()
        except Exception as exc:
            with app.app_context():
                db.session.rollback()
            logger.warning("Storico locale non salvato: %s", exc)
        try:
            salva_ricerca(user_id, query, result)
        except Exception as exc:
            logger.warning("Risultato non salvato nel database: %s", exc)
    except Exception as exc:
        logger.exception("Ricerca %s fallita", task_id)
        with _jobs_lock:
            jobs[task_id] = {
                "status": "failed", "error": str(exc), "user_id": user_id
            }
    finally:
        with _jobs_lock:
            _active_searches[user_id] = max(0, _active_searches.get(user_id, 1) - 1)


@app.route('/api/search', methods=['POST'])
@login_required
@csrf_required
def start_search():
    data = request.get_json(silent=True) or {}
    query = clean(data.get('query'))
    if not query:
        return jsonify({'error': 'Codice vuoto'}), 400
    if len(query) > 100:
        return jsonify({'error': 'Codice troppo lungo'}), 400

    task_id = str(uuid.uuid4())
    user_id = session['user_id']
    with _jobs_lock:
        if _active_searches.get(user_id, 0) >= 2:
            return jsonify({'error': 'Hai già 2 ricerche in corso'}), 429
        _active_searches[user_id] = _active_searches.get(user_id, 0) + 1
        jobs[task_id] = {'status': 'pending', 'user_id': user_id}
    try:
        search_executor.submit(_search_worker, task_id, query, user_id)
    except Exception:
        with _jobs_lock:
            jobs.pop(task_id, None)
            _active_searches[user_id] = max(0, _active_searches[user_id] - 1)
        raise
    return jsonify({'task_id': task_id}), 202

#
def avvia_scraping_siti(codice):
    sites = load_sites()
    results = run_sites(sites, codice)

    return {
        "found_sites": sum(r["found"] for r in results),
        "total_sites": len(results),
        "price_sites": sum(r["verified_prices"] for r in results),
        "results": results,
        "comparison": build_comparison(results),
    }

@app.route('/api/search/status/<task_id>', methods=['GET'])
@login_required
def check_status(task_id):
    job = jobs.get(task_id)
    if not job or job.get('user_id') != session['user_id']:
        return jsonify({'status': 'failed', 'error': 'Task non trovato'}), 404
    return jsonify({key: value for key, value in job.items() if key != 'user_id'})


#verifica l'URL, aggiunge http/https se mancano
def normalize_base(url):
    url=clean(url)
    if not url:return None
    if not url.startswith(("http://","https://")): url="https://"+url
    try:
        p=urlparse(url)
        return f"{p.scheme}://{p.netloc}" if p.netloc else None
    except:return None


def load_sites():
    """Carica la configurazione dei fornitori senza rendere inutilizzabile l'app."""
    path = os.path.join(ROOT, "sites.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Impossibile leggere %s: %s", path, exc)
        return []
    if not isinstance(data, list):
        logger.error("%s deve contenere una lista", path)
        return []
    return [
        {"name": clean(item.get("name")), "url": clean(item.get("url"))}
        for item in data
        if isinstance(item, dict) and clean(item.get("url"))
    ]


def build_dashboard(user_id):
    """Prepara statistiche reali e serializzabili per la pagina principale."""
    searches = []
    seen_codes = set()

    for key, result in reversed(list(saved_searches.items())):
        if not isinstance(key, tuple) or key[0] != user_id or not isinstance(result, dict):
            continue
        code = clean(result.get("code") or key[1]).upper()
        if code and code not in seen_codes:
            searches.append((code, result, result.get("completed_at")))
            seen_codes.add(code)

    try:
        local_rows = SearchHistory.query.filter_by(user_id=user_id).order_by(
            SearchHistory.updated_at.desc()
        ).limit(30).all()
    except Exception as exc:
        logger.debug("Storico locale non disponibile: %s", exc)
        local_rows = []
    for row in local_rows:
        code = clean(row.code).upper()
        if code and code not in seen_codes and isinstance(row.result, dict):
            searches.append((code, row.result, row.updated_at))
            seen_codes.add(code)

    try:
        stored = get_ricerche_recenti(user_id, 30)
    except Exception as exc:
        logger.debug("Storico non disponibile per la home: %s", exc)
        stored = []
    for code, result, updated_at in stored:
        code = clean(code).upper()
        if code and code not in seen_codes and isinstance(result, dict):
            searches.append((code, result, updated_at))
            seen_codes.add(code)

    products = []
    total_offers = 0
    for code, result, updated_at in searches:
        prices = []
        result_sites = result.get("results") or []
        for site in result_sites:
            if not isinstance(site, dict):
                continue
            values = list((site.get("neutral") or {}).values())
            values += list((site.get("printed") or {}).values())
            if site.get("generic") is not None:
                values.append(site["generic"])
            for value in values:
                try:
                    prices.append(float(value))
                except (TypeError, ValueError):
                    pass
        total_offers += len(prices)
        best = min(prices) if prices else None
        highest = max(prices) if prices else None
        saving = round((highest - best) / highest * 100, 1) if best is not None and highest and highest > best else None
        moment = updated_at or result.get("completed_at")
        if isinstance(moment, str):
            try:
                moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
            except ValueError:
                moment = None
        date_label = moment.astimezone().strftime("%d/%m/%Y · %H:%M") if isinstance(moment, datetime) else "Data non disponibile"
        products.append({
            "code": code,
            "date": date_label,
            "found_sites": int(result.get("found_sites") or 0),
            "total_sites": int(result.get("total_sites") or len(result_sites)),
            "price_sites": int(result.get("price_sites") or 0),
            "best_price": best,
            "highest_price": highest,
            "saving": saving,
            "results": result_sites,
        })

    return {
        "products": products,
        "product_count": len(products),
        "offer_count": total_offers,
        "company_count": len(load_sites()),
    }

#estrae il dominio dall'URL e rimuove il prefisso www
def domain(url):
    try:return urlparse(url).netloc.lower().replace("www.","")
    except:return ""


def is_safe_public_url(url):
    """Blocca protocolli non web e destinazioni locali/private (protezione SSRF)."""
    try:
        parsed = urlparse(clean(url))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname == "localhost" or hostname.endswith(".localhost"):
            return False
        try:
            addresses = [ipaddress.ip_address(hostname)]
        except ValueError:
            addresses = [
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
            ]
        return bool(addresses) and all(address.is_global for address in addresses)
    except (OSError, ValueError):
        return False

#verifica se due URL appartengono allo stesso dominio o a un sotto-dominio
def same_domain(url,base):
    a,b=domain(url),domain(base)
    return bool(a and b and (a==b or a.endswith("."+b)))

#controlla se l'URL inserito corrisponde alla home page
def is_home(url):
    try:
        p=urlparse(url)
        return not (p.path or "/").strip("/") and not p.query
    except:return True

#controlla se l'URL mostra una lista di prodotti o un risultato di ricerca
def is_listing(url):
    low=(url or "").lower()
    return any(x in low for x in ("/search","catalogsearch","/cerca","?q=","&q=","?s=","&s=","/ricerca","/results"))

#scarica il contenuto della pagina web dall'URL fornito
def get(url):
    return requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)

#estrae e pulisce il prezzo numerico da un testo generico
def parse_price(raw):
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        return round(v, 4) if 0.01 <= v < 100000 else None

    s = clean(raw).replace("\xa0", " ").strip()
    number = r"\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,4})?|\d{1,3}(?:,\d{3})*(?:\.\d{1,4})?|\d{1,6}(?:[.,]\d{1,4})?"
    patterns = (
        rf"(?:prezzo|price|costo|cost)\s*(?:unitario|unit|cad\.?|cadauno)?\s*[:=]?\s*(?:€|eur)?\s*(?P<n>{number})",
        rf"(?:€|eur)\s*(?P<n>{number})",
        rf"(?P<n>{number})\s*(?:€|eur)(?!\w)",
    )
    match = next((found for pattern in patterns if (found := re.search(pattern, s, re.I))), None)
    if match:
        n = match.group("n")
    elif re.fullmatch(rf"\s*{number}\s*", s):
        n = s
    else:
        # Nei listini il simbolo valuta può mancare, ma un valore decimale è
        # molto più plausibile come prezzo di un codice SKU o una quantità.
        match = re.search(r"(?<!\d)(\d{1,6}[.,]\d{1,4})(?!\d)", s)
        if not match:
            return None
        n = match.group(1)
    n = n.replace(" ", "")

    #gestione combinata di virgola e punto
    if "," in n and "." in n:
        n = n.replace(".", "").replace(",", ".") if n.rfind(",") > n.rfind(".") else n.replace(",", "")
    elif "," in n:
        parts = n.split(",")
        # Se ci sono 3 cifre esatte dopo la virgola, è un separatore di migliaia (es: 1,500)
        if len(parts) > 1 and len(parts[-1]) == 3 and len(parts[0]) <= 3:
            n = n.replace(",", "")
        else:
            n = n.replace(",", ".")
    elif "." in n:
        parts = n.split(".")
        # Se ci sono 3 cifre esatte dopo il punto, è un separatore di migliaia (es: 1.500 €)
        if len(parts) > 1 and len(parts[-1]) == 3 and len(parts[0]) <= 3:
            n = n.replace(".", "")

    try:
        v = float(n)
        return round(v, 4) if 0.01 <= v < 100000 else None
    except ValueError:
        return None

# trova e legge il numero di pezzi o quantità nel testo
def qty_start(raw):
    s=clean(raw).lower()
    for pat in (
        r"^(\d{1,6})\s*(?:pz|pcs|pezzi|unità|unita)?$",
        r"^(\d{1,6})\s*[-–]\s*\d{1,6}$",
        r"^(\d{1,6})\s*\+$",
        r"^(?:>=|≥)\s*(\d{1,6})$"
    ):
        m=re.match(pat,s,re.I)
        if m:
            q=int(m.group(1))
            if 1<=q<=100000:return q
    return None

#trova il titolo principale della pagina web
def page_title(soup):
    h=soup.find("h1")
    if h:return clean(h.get_text(" ",strip=True))
    return clean(soup.title.get_text(" ",strip=True)) if soup.title else ""


# ---------------- EXACT SEARCH ASSIST ----------------

STOPWORDS = {
    "per","con","senza","del","della","dello","dei","degli","delle","da","di","in","il","lo","la","i","gli","le",
    "a","al","alla","ai","alle","e","ed","o","un","uno","una","the","for","with","without","and","or","of","to",
    "prodotto","product","personalizzato","personalizzabile","personalizzazione","stampa","printing","custom"
}

#crea i diversi modi in cui può essere scritto un codice prodotto
def code_variants(code):
    """
    Universal supplier codes are often written differently by ecommerce sites:
    MO9833 / MO-9833 / MO 9833.
    """
    raw = clean(code).upper()
    compact = norm_code(raw)
    out = [raw, compact]
    m = re.match(r"^([A-Z]+)(\d+)$", compact)
    if m:
        out += [f"{m.group(1)}-{m.group(2)}", f"{m.group(1)} {m.group(2)}"]
    return list(dict.fromkeys(x for x in out if x))

#divide il titolo in singole parole utili ed esclude quelle inutili
def title_tokens(title):
    words = re.findall(r"[A-Za-zÀ-ÿ0-9]{3,}", clean(title).lower())
    out = []
    for w in words:
        if w in STOPWORDS or w.isdigit():
            continue
        if re.fullmatch(r"[a-z]{1,2}\d+", w):
            continue
        out.append(w)
    return list(dict.fromkeys(out))[:12]

#cerca ed estrae le misure e le dimensioni presenti nel testo
def extract_dimensions(text):
    """
    Normalize dimensions such as 22 x 18 x 0,2 cm / 220x180 mm.
    """
    found = []
    for m in re.finditer(
        r"(\d{1,4}(?:[.,]\d+)?)\s*[x×]\s*(\d{1,4}(?:[.,]\d+)?)(?:\s*[x×]\s*(\d{1,4}(?:[.,]\d+)?))?\s*(mm|cm)?",
        text or "", re.I
    ):
        vals = [m.group(1), m.group(2), m.group(3)]
        unit = (m.group(4) or "").lower()
        nums = []
        for v in vals:
            if not v:
                continue
            try:
                n = float(v.replace(",", "."))
                if unit == "cm":
                    n *= 10
                nums.append(round(n, 2))
            except:
                pass
        if len(nums) >= 2:
            found.append("x".join(str(int(n)) if float(n).is_integer() else str(n) for n in nums))
    return list(dict.fromkeys(found))[:5]

#genera i dati identificativi del prodotto per poterlo riconoscere
def build_fingerprint(product, code):
    if not product:
        return {"code": norm_code(code), "tokens": [], "dimensions": []}
    title = product.get("title") or ""
    text = product.get("text") or ""
    return {
        "code": norm_code(code),
        "tokens": title_tokens(title),
        "dimensions": extract_dimensions(title + " " + text[:5000])
    }

#calcola un punteggio da 0 a 100 per verificare l'affidabilità del prodotto trovato
def fingerprint_score(product, code, fingerprint):
    """
    Score is only a confidence indicator. Exact code remains the primary requirement.
    """
    if not product:
        return 0, []
    hay_title = clean(product.get("title")).lower()
    hay_text = clean(product.get("text")).lower()
    hay_url = clean(product.get("url")).lower()
    reasons = []
    score = 0

    variants = code_variants(code)
    exact_code = any(v.lower() in (hay_title + " " + hay_text + " " + hay_url) for v in variants)
    if exact_code:
        score += 70
        reasons.append("codice esatto")

    tokens = fingerprint.get("tokens") or []
    matched_tokens = [t for t in tokens if t in hay_title or t in hay_text[:6000]]
    if matched_tokens:
        add = min(20, len(matched_tokens) * 4)
        score += add
        reasons.append(f"{len(matched_tokens)} parole prodotto")

    dims = fingerprint.get("dimensions") or []
    candidate_dims = extract_dimensions(hay_title + " " + hay_text[:6000])
    if dims and candidate_dims and set(dims).intersection(candidate_dims):
        score += 10
        reasons.append("dimensioni compatibili")

    return min(score, 100), reasons

#crea un testo di ricerca avnzato unendo il codice e le parole del titolo
def enhanced_search_query(base, code, fingerprint=None):
    d = domain(base)
    parts = [f'site:{d}', f'"{clean(code)}"']
    if fingerprint:
        for token in (fingerprint.get("tokens") or [])[:3]:
            parts.append(f'"{token}"')
    return " ".join(parts)


#salva i risultati del match in postgres
def salva_il_match(prodotto_id, sito_id, prezzo, score_affidabilita, url_prodotto):
    try:
        conn = get_connection()  #la funzione di connessione
        cur = conn.cursor()

        query = """
            INSERT INTO risultati_match 
            (prodotto_id, sito_id, prezzo, score_affidabilita, url_prodotto, data_rilevazione)
            VALUES (%s, %s, %s, %s, %s, NOW());
        """
        cur.execute(query, (prodotto_id, sito_id, prezzo, score_affidabilita, url_prodotto))
        
        conn.commit()  #senza questo Postgres annulla l'inserimento!
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Errore durante il salvataggio su DB: {e}")
# ---------------- CACHE ----------------

#previene la corruzione del file di cache
def safe_save_json(filepath, data):
    folder=os.path.dirname(filepath) or "."
    with tempfile.NamedTemporaryFile(
        "w", dir=folder, delete=False, encoding="utf-8"
    ) as tf:
        json.dump(data, tf, ensure_ascii=False, indent=2)
        temp_name=tf.name
    os.replace(temp_name, filepath)

#legge e carica in memoria tutti i dati salvati nel file della cache
PENDING_CACHE_FILE = os.path.join(ROOT, "product_cache_pending.json")

def cache_load():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

#cerca e recupera un prodotto dalla cache usando il dominio e il codice
def cache_get(base,code):
    key=domain(base)+"|"+norm_code(code)
    with _cache_lock:
        return cache_load().get(key)

#salva un nuovo prodotto dentro il filedella cache per non cercarlo di nuovo
def cache_set(base,code,url,title):
    key=domain(base)+"|"+norm_code(code)
    with _cache_lock:
        data=cache_load()
        data[key]={"url":url,"title":title}
        try:
            safe_save_json(CACHE_FILE,data)
        except OSError:
            pass

#rimuove in modo definitivo un prodotto specifico dal file della cache
def cache_delete(base,code):
    key=domain(base)+"|"+norm_code(code)
    with _cache_lock:
        data=cache_load()
        if key in data:
            data.pop(key,None)
            try:
                safe_save_json(CACHE_FILE,data)
            except OSError:
                pass

#controlla se esite già una ricerca completa per la query richiesta
@app.route('/api/search/check', methods=['GET'])
@login_required
def check_search_exists():
    query = request.args.get('query', '').strip().upper()
    key = (session['user_id'], query)
    has_prev = key in saved_searches
    if query and not has_prev:
        try:
            previous = get_ricerca_precedente(session['user_id'], query)
        except Exception:
            previous = None
        if previous:
            saved_searches[key] = previous
            has_prev = True
    return jsonify({"has_previous": has_prev})

#restituisce imediatamente i risultati della ricerca precedente
@app.route('/api/search/previous', methods=['GET'])
@login_required
def get_previous_search():
    query = request.args.get('query', '').strip().upper()
    key = (session['user_id'], query)
    if key in saved_searches:
        return jsonify(saved_searches[key])
    try:
        previous = get_ricerca_precedente(session['user_id'], query) if query else None
    except Exception:
        previous = None
    if previous:
        saved_searches[key] = previous
        return jsonify(previous)
    return jsonify({"error": "Nessuna ricerca precedente trovata"}), 404


# ---------------- PRODUCT VERIFICATION ----------------

#controlla se il codice è presente nell'URl, nel titolo o nel testo del sito
@lru_cache(maxsize=4096)
def rx_code(code):

    """
    MO9833 trova:
    MO9833, MO-9833, MO 9833, MO_9833, MO.9833
    senza trovare MO98330 o XMO9833.
    """
    compact = norm_code(clean(code).upper())
    if not compact:
        return None

    # Divide il codice nei passaggi lettere/numeri.
    # Esempio: AB12CD34 -> AB, 12, CD, 34
    parts = re.findall(r"[A-Z]+|\d+", compact)

    separator = r"[\s\-_.:/]*"
    body = separator.join(re.escape(part) for part in parts)

    return re.compile(
        rf"(?<![A-Z0-9]){body}(?![A-Z0-9])",
        re.IGNORECASE,
    )
_CODE_SELECTORS = ",".join((
    '[itemprop="sku"]',
    '[itemprop="mpn"]',
    '[name="sku"]',
    '[name="mpn"]',
    '[data-sku]',
    '[data-mpn]',
    '[data-product-code]',
    '[data-code]',
))
def exact_code_present(soup, url, code):
    target = norm_code(clean(code).upper())
    if not target:
        return False

    rx = rx_code(code)
    if rx is None:
        return False

    title = page_title(soup) or ""
    decoded_url = unquote_plus(unquote_plus(url or ""))

    # Controlli veloci e generalmente affidabili
    if rx.search(decoded_url) or rx.search(title):
        return True

    text = clean(soup.get_text(" ", strip=True))

    # Codice preceduto da un'etichetta significativa
    label_rx = re.compile(
        rf"""
        \b(?:
            cod(?:ice)? |
            sku |
            mpn |
            ref(?:erence)? |
            art(?:icolo)? |
            product\s*code |
            item\s*(?:number|no)
        )\b
        [\s:#=\-]{{0,20}}
        {rx.pattern}
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    if label_rx.search(text):
        return True

    # Attributi HTML strutturati
    attributes = (
        "content",
        "value",
        "data-sku",
        "data-mpn",
        "data-product-code",
        "data-code",
    )

    for element in soup.select(_CODE_SELECTORS):
        values = [element.get(attribute) for attribute in attributes]
        values.append(element.get_text(" ", strip=True))

        if any(
            norm_code(clean(value).upper()) == target
            for value in values
            if value
        ):
            return True

    # Nei JSON-LD il codice deve essere associato a un campo identificativo:
    # una semplice citazione tra prodotti correlati non basta.
    for script in soup.select('script[type*="ld+json"]'):
        script_text = script.string or script.get_text(" ", strip=True)
        structured_rx = re.compile(
            rf'"(?:sku|mpn|productID)"\s*:\s*"{rx.pattern}"',
            re.IGNORECASE,
        )
        if script_text and structured_rx.search(script_text):
            return True
    return False

#scarica una pagina e verifica se è valida, se è la home o se contiene il codice cercato
def verify_url(base,url,code):
    try:
        r=get(url)
        if not r.ok or "html" not in (r.headers.get("content-type") or "").lower():return None
        if is_home(r.url) or is_listing(r.url):return None
        soup=BeautifulSoup(r.text,"html.parser")
        if not exact_code_present(soup,r.url,code):return None
        final=r.url.split("#")[0]
        tag=soup.find("link",rel=lambda x:x and "canonical" in x)
        if tag and tag.get("href"):
            c=urljoin(r.url,tag["href"]).split("#")[0]
            if same_domain(c,base) and not is_home(c) and not is_listing(c):final=c
        return {"url":final,"title":page_title(soup) or code,"soup":soup,"text":clean(soup.get_text(" ",strip=True))}
    except:return None

# ---------------- FAST RESOLVER ----------------

#trova ed estrae tutti i link utili presenti all'interno della pagina web
def extract_links(r,base,code,allow_price_cards=False):
    out=[];rx=rx_code(code)
    try:
        soup=BeautifulSoup(r.text,"html.parser")
        # Direct redirect to a product page.
        if same_domain(r.url,base) and not is_home(r.url) and not is_listing(r.url):
            if exact_code_present(soup,r.url,code):out.append(r.url.split("#")[0])
        for a in soup.find_all("a",href=True):
            href=urljoin(r.url,a["href"])
            if not same_domain(href,base) or is_home(href) or is_listing(href):continue
            container=a
            for _ in range(3):
                if container.parent:container=container.parent
            context=clean(container.get_text(' ',strip=True))
            hay=f"{href} {clean(a.get_text(' ',strip=True))} {context}"
            has_price=bool(re.search(r"(?:€\s*\d|\d+[.,]\d+\s*(?:€|eur)|prezzo\s+per)",context,re.I))
            if rx.search(hay) or (allow_price_cards and has_price):
                out.append(href.split("#")[0])
    except:pass
    return list(dict.fromkeys(out))

#cerca e usa le barre di ricerca reali dei siti per trovare il prodotto
def real_search_candidates(base,code):
    out=[]
    try:
        r=get(base);soup=BeautifulSoup(r.text,"html.parser")
        forms=[]
        for form in soup.find_all("form"):
            best=None
            for inp in form.find_all("input"):
                name=inp.get("name")
                if not name:continue
                typ=(inp.get("type") or "").lower()
                hint=((inp.get("placeholder") or "")+" "+(inp.get("aria-label") or "")).lower()
                score=0
                if typ=="search":score+=12
                if name.lower() in {"q","s","search","query","keyword","search_query","term","searchterm"}:score+=9
                if any(k in hint for k in ("cerca","search","trova")):score+=7
                if score and (best is None or score>best[0]):best=(score,name)
            if best:
                forms.append(((form.get("method") or "get").lower(),urljoin(base,form.get("action") or base),best[1]))
        for method,url,param in forms[:4]:
            try:
                rr=requests.post(url,data={param:code},headers=HEADERS,timeout=TIMEOUT,allow_redirects=True) if method=="post" else get(url+("&" if "?" in url else "?")+urlencode({param:code}))
                out.extend(extract_links(rr,base,code,allow_price_cards=True))
            except:pass
    except:pass

    routes=[
        (base.rstrip("/")+"/search","q"),(base.rstrip("/")+"/search","query"),
        (base.rstrip("/")+"/catalogsearch/result/","q"),(base.rstrip("/")+"/cerca","q"),
        (base.rstrip("/")+"/","s")
    ]
    for url,param in routes:
        if out:break
        try:
            rr=get(url+("&" if "?" in url else "?")+urlencode({param:code}))
            out.extend(extract_links(rr,base,code,allow_price_cards=True))
        except:pass
    return list(dict.fromkeys(out))[:10]

#usa bing in modalità nascosta per cercare i link del prodotto sul sito
def bing_rss_candidates(base,code,fingerprint=None):
    d=domain(base)
    if not d:return []
    queries = [enhanced_search_query(base, code, fingerprint)]
    queries.append(f'site:{d} "{code}"')
    out=[]
    for query in queries:
        try:
            q=quote_plus(query)
            r=get("https://www.bing.com/search?format=rss&q="+q)
            root=ElementTree.fromstring(r.content)
            for item in root.findall(".//item")[:12]:
                node=item.find("link")
                link=clean(node.text) if node is not None else ""
                if same_domain(link,base) and not is_home(link):
                    out.append(link.split("#")[0])
            if out:
                break
        except:
            pass
    return list(dict.fromkeys(out))

#cerca il prodotto prima in memoria e poiavviando le ricerche automatiche sul sito e su bing
def fast_resolve(base,code,fingerprint=None):
    cached=cache_get(base,code)
    if cached:
        p=verify_url(base,cached.get("url",""),code)
        if p:
            p["source"]="cache"
            return p
        cache_delete(base,code)

    candidates=[]
    for variant in code_variants(code):
        try:
            candidates.extend(("ricerca sito", u) for u in real_search_candidates(base,variant))
        except:
            pass

    try:
        candidates.extend(("Bing esatto", u) for u in bing_rss_candidates(base,code,fingerprint))
    except:
        pass

    seen=set()
    verified=[]
    for source,u in candidates:
        if u in seen:
            continue
        seen.add(u)
        p=verify_url(base,u,code)
        if p:
            p["source"]=source
            score,reasons=fingerprint_score(p,code,fingerprint or {})
            p["match_score"]=score
            p["match_reasons"]=reasons
            verified.append(p)

    if verified:
        verified.sort(key=lambda p:p.get("match_score",0),reverse=True)
        p=verified[0]
        cache_set(base,code,p["url"],p["title"])
        return p
    return None

# ---------------- BROWSER RESOLVER (V6 LOGIC, REUSED) ----------------

#installa automaticamente il browser Chromium se non è presente
def ensure_chromium():
    global _install_attempted
    with _install_lock:
        if _install_attempted:return
        _install_attempted=True
        try:
            subprocess.run([sys.executable,"-m","playwright","install","chromium"],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=240)
        except:pass

#avvia il browser playwright in modalità nascosta per navigare sui siti
def open_browser():
    from playwright.sync_api import sync_playwright
    p=sync_playwright().start()
    try:browser=p.chromium.launch(headless=True)
    except:
        ensure_chromium()
        browser=p.chromium.launch(headless=True)
    return p,browser

#estrae e ripulisce tutto il testo visibile all'interno della pagina web
def browser_body(page):
    try:return clean(page.locator("body").inner_text(timeout=4500))
    except:return ""

#trova il titolo principale cercando prima il tag e poi il titolo della scheda
def browser_title(page):
    try:
        h=page.locator("h1").first
        if h.count():
            t=clean(h.inner_text(timeout=800))
            if t:return t
    except:pass
    try:return clean(page.title())
    except:return ""

#controlla se il codice cercato esiste nell'URL, nel titolo o nel browser
def browser_code_present(page,code):
    rx=rx_code(code)
    return bool(rx.search(page.url+" "+browser_title(page)+" "+browser_body(page)))

#racoglie i link della pagina ordinandoli in base a dove appare il codice prodotto
def collect_browser_links(page,base,code):
    try:
        items=page.locator("a[href]").evaluate_all("""
          els => els.slice(0,2600).map(a => {
            let p=a;
            for(let i=0;i<3 && p.parentElement;i++) p=p.parentElement;
            return {href:a.href||'', text:(a.innerText||'').trim(), context:(p.innerText||'').trim().slice(0,1000)};
          })
        """)
    except:return []
    rx=rx_code(code);scored=[]
    for x in items:
        href=x.get("href","")
        if not same_domain(href,base) or is_home(href):continue
        hay=f"{href} {x.get('text','')} {x.get('context','')}"
        if not rx.search(hay):continue
        score=(5 if rx.search(href) else 0)+(4 if rx.search(x.get("text","")) else 0)+(2 if rx.search(x.get("context","")) else 0)
        scored.append((score,href.split("#")[0]))
    scored.sort(reverse=True)
    return list(dict.fromkeys(u for _,u in scored))[:12]

#cerca la barra di ricerca del sito, scrive il codice prodotto e preme invio poi apre la pagina di bing, cerca il prodotto e analizza i primi 10 link trovati
def browser_resolve_one(browser,base,code,fingerprint=None):
    ctx=browser.new_context(locale="it-IT",viewport={"width":1365,"height":950},user_agent=HEADERS["User-Agent"])
    try:
        # A) Search inside the site.
        page=ctx.new_page()
        try:
            page.goto(base,wait_until="domcontentloaded",timeout=BROWSER_TIMEOUT)
            page.wait_for_timeout(650)
            selectors=[
                'input[type="search"]','input[name="q"]','input[name="s"]','input[name="search"]',
                'input[name="query"]','input[name="keyword"]','input[placeholder*="cerca" i]',
                'input[placeholder*="search" i]','input[aria-label*="cerca" i]','input[aria-label*="search" i]'
            ]
            field=None
            for sel in selectors:
                try:
                    loc=page.locator(sel).first
                    if loc.count() and loc.is_visible(timeout=200):
                        field=loc;break
                except:pass
            if field:
                try:
                    # Start with the canonical code; variants are used below if needed.
                    field.fill(code);field.press("Enter");page.wait_for_timeout(1100)
                    if not is_home(page.url) and not is_listing(page.url) and browser_code_present(page,code):
                        html=page.content();soup=BeautifulSoup(html,"html.parser")
                        final=page.url.split("#")[0]
                        result={"url":final,"title":browser_title(page) or code,"soup":soup,"text":browser_body(page),"source":"browser sito"}
                        score,reasons=fingerprint_score(result,code,fingerprint or {})
                        result["match_score"]=score; result["match_reasons"]=reasons
                        cache_set(base,code,final,result["title"])
                        page.close();return result
                    for u in collect_browser_links(page,base,code):
                        p2=ctx.new_page()
                        try:
                            p2.goto(u,wait_until="domcontentloaded",timeout=BROWSER_TIMEOUT);p2.wait_for_timeout(500)
                            if not is_home(p2.url) and not is_listing(p2.url) and browser_code_present(p2,code):
                                html=p2.content();soup=BeautifulSoup(html,"html.parser");final=p2.url.split("#")[0]
                                result={"url":final,"title":browser_title(p2) or code,"soup":soup,"text":browser_body(p2),"source":"browser sito"}
                                score,reasons=fingerprint_score(result,code,fingerprint or {})
                                result["match_score"]=score; result["match_reasons"]=reasons
                                cache_set(base,code,final,result["title"]);p2.close();page.close();return result
                        except:pass
                        try:p2.close()
                        except:pass
                except:pass
        except:pass
        try:page.close()
        except:pass

        # B) Bing normal web page, exactly like the older working approach.
        page=ctx.new_page()
        try:
            q=quote_plus(enhanced_search_query(base,code,fingerprint))
            page.goto("https://www.bing.com/search?q="+q,wait_until="domcontentloaded",timeout=BROWSER_TIMEOUT)
            page.wait_for_timeout(850)
            links=[]
            try:
                items=page.locator("li.b_algo h2 a[href]").evaluate_all("(els)=>els.map(a=>a.href||'')")
                links.extend([u for u in items if same_domain(u,base)])
            except:pass
            if not links:
                links.extend(collect_browser_links(page,base,code))
            for u in list(dict.fromkeys(links))[:10]:
                p2=ctx.new_page()
                try:
                    p2.goto(u,wait_until="domcontentloaded",timeout=BROWSER_TIMEOUT);p2.wait_for_timeout(500)
                    if not is_home(p2.url) and not is_listing(p2.url) and browser_code_present(p2,code):
                        html=p2.content();soup=BeautifulSoup(html,"html.parser");final=p2.url.split("#")[0]
                        result={"url":final,"title":browser_title(p2) or code,"soup":soup,"text":browser_body(p2),"source":"browser Bing"}
                        score,reasons=fingerprint_score(result,code,fingerprint or {})
                        result["match_score"]=score; result["match_reasons"]=reasons
                        cache_set(base,code,final,result["title"]);p2.close();page.close();return result
                except:pass
                try:p2.close()
                except:pass
        except:pass
        try:page.close()
        except:pass
        return None
    finally:
        ctx.close()


# ---------------- BROWSER-FIRST EXACT CODE RESOLVER ----------------

#lista di codici per trovare la casella di testo dove scrivere la ricerca sul sito
SEARCH_INPUT_SELECTORS = [
    'input[type="search"]',
    'input[name="q"]',
    'input[name="s"]',
    'input[name="search"]',
    'input[name*="search" i]',
    'input[id*="search" i]',
    'input[class*="search" i]',
    'input[name="query"]',
    'input[name="keyword"]',
    'input[name="term"]',
    'input[placeholder*="cerca" i]',
    'input[placeholder*="search" i]',
    'input[aria-label*="cerca" i]',
    'input[aria-label*="search" i]'
]

#lista di codici per trovare il pulsante "Cerca" o la lente d'ingrandimento da cliccare
SEARCH_TRIGGER_SELECTORS = [
    'button[aria-label*="cerca" i]',
    'button[aria-label*="search" i]',
    '[role="button"][aria-label*="cerca" i]',
    '[role="button"][aria-label*="search" i]',
    'button:has-text("Cerca")',
    'button:has-text("Search")'
]

#verifica se il codice cercato è scritto nella pagina o nei dati nascosti del browser
def rendered_code_present(page, code):
    """
    Verify the supplier code in the actually rendered page.
    Accept MO9833 / MO-9833 / MO 9833, but not generic name similarity.
    """
    try:
        title = clean(page.title())
    except:
        title = ""
    try:
        body = clean(page.locator("body").inner_text(timeout=5000))
    except:
        body = ""
    hay = f"{page.url} {title}"

    for variant in code_variants(code):
        if re.search(rf"(?<![A-Z0-9]){re.escape(variant)}(?![A-Z0-9])", hay, re.I):
            return True

    # Structured DOM attributes.
    selectors = [
        '[itemprop="sku"]','[itemprop="mpn"]','[data-sku]',
        '[data-product-code]','[data-code]','[data-product-sku]'
    ]
    target = norm_code(code)
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = min(loc.count(), 30)
            for i in range(count):
                el = loc.nth(i)
                vals = []
                for attr in ("content","data-sku","data-product-code","data-code","data-product-sku"):
                    try:
                        vals.append(el.get_attribute(attr))
                    except:
                        pass
                try:
                    vals.append(el.inner_text(timeout=300))
                except:
                    pass
                if any(norm_code(v) == target for v in vals if v):
                    return True
        except:
            pass
    rx = rx_code(code)
    if rx:
        label_rx = re.compile(
            rf"\b(?:cod(?:ice)?|sku|mpn|ref(?:erence)?|art(?:icolo)?|product\s*code|item\s*(?:number|no))\b"
            rf"[\s:#=\-]{{0,20}}{rx.pattern}",
            re.IGNORECASE,
        )
        if label_rx.search(body):
            return True
    return False

#raccoglie tutti i dati verificati della pagina e crea la scheda finale del prodotto
def rendered_product_payload(page, code, source):
    if is_home(page.url) or is_listing(page.url):
        return None
    if not rendered_code_present(page, code):
        return None

    try:
        html = page.content()
    except:
        return None
    soup = BeautifulSoup(html, "html.parser")
    text = browser_body(page)
    title = browser_title(page) or page_title(soup) or code

    final = page.url.split("#")[0]
    try:
        tag = soup.find("link", rel=lambda x: x and "canonical" in x)
        if tag and tag.get("href"):
            c = urljoin(page.url, tag["href"]).split("#")[0]
            if same_domain(c, page.url) and not is_home(c) and not is_listing(c):
                final = c
    except:
        pass

    return {
        "url": final,
        "title": title,
        "soup": soup,
        "text": text,
        "source": source,
        "match_score": 100,
        "match_reasons": ["codice verificato nella pagina"]
    }

#cerca e individua la barra di ricerca nella pagina, cliccando l'icona se è nascosta
def browser_find_search_field(page):
    for sel in SEARCH_INPUT_SELECTORS:
        try:
            matches = page.locator(sel)
            for index in range(min(matches.count(), 8)):
                loc = matches.nth(index)
                if loc.is_visible(timeout=250):
                    return loc
        except:
            pass

    # Some ecommerce sites hide the input behind a search icon.
    for trigger in SEARCH_TRIGGER_SELECTORS:
        try:
            btn = page.locator(trigger).first
            if btn.count() and btn.is_visible(timeout=200):
                btn.click(timeout=600)
                page.wait_for_timeout(300)
                for sel in SEARCH_INPUT_SELECTORS:
                    try:
                        matches = page.locator(sel)
                        for index in range(min(matches.count(), 8)):
                            loc = matches.nth(index)
                            if loc.is_visible(timeout=250):
                                return loc
                    except:
                        pass
        except:
            pass
    return None

#estrae eassegna un punteggio ai link della pagina in base alla vicinanza del codice prodotto
def browser_candidate_links(page, base, code):
    try:
        items = page.locator("a[href]").evaluate_all("""
            els => els.slice(0,3500).map(a => {
                let p = a;
                for (let i=0; i<4 && p.parentElement; i++) p = p.parentElement;
                return {
                    href: a.href || '',
                    text: (a.innerText || '').trim(),
                    context: (p.innerText || '').trim().slice(0,1400)
                };
            })
        """)
    except:
        return []

    variants = [v.lower() for v in code_variants(code)]
    scored = []
    for x in items:
        href = x.get("href","")
        if not same_domain(href, base) or is_home(href):
            continue
        hay = f"{href} {x.get('text','')} {x.get('context','')}".lower()
        score = 0
        for v in variants:
            if v in href.lower():
                score += 10
            if v in x.get("text","").lower():
                score += 8
            if v in x.get("context","").lower():
                score += 5
        if score:
            if not is_listing(href):
                score += 3
            scored.append((score, href.split("#")[0]))

    scored.sort(key=lambda t: t[0], reverse=True)
    return list(dict.fromkeys(u for _,u in scored))[:MAX_INTERNAL_RESULTS]

#apre un indirizzo, attende il caricamento e salva il prodotto in memoria se valido
def browser_open_verified(ctx, url, base, code, source):
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
        try:
            page.wait_for_load_state("networkidle", timeout=3500)
        except:
            page.wait_for_timeout(700)
        payload = rendered_product_payload(page, code, source)
        if payload:
            cache_set(base, code, payload["url"], payload["title"])
            return payload
    except:
        pass
    finally:
        try:
            page.close()
        except:
            pass
    return None

#gestisce la ricerca completa del prodotto usando cache, sito interno e bing nel browser
import time

def browser_resolve_exact(browser, base, code):
    deadline = time.monotonic() + SITE_TIMEOUT

    def expired():
        return time.monotonic() >= deadline

    """
    This is the primary resolver in V15.
    1) Verify cached URL in a real browser.
    2) Enter the ecommerce homepage.
    3) Type the supplier code into the site's own search.
    4) Open candidate result pages and verify the code in rendered DOM.
    5) Only then use Bing in-browser as fallback.
    """
    ctx = browser.new_context(
        locale="it-IT",
        viewport={"width":1365,"height":950},
        user_agent=HEADERS["User-Agent"]
    )
    try:
        # 1. Cache is only a shortcut to the URL; code and prices are re-read live.
        cached = cache_get(base, code)
        if cached and cached.get("url"):
            p = browser_open_verified(
                ctx, cached["url"], base, code, "browser - URL gia nota"
            )
            if p:
                return p
            cache_delete(base, code)

        # 2. Real site search.
        page = ctx.new_page()
        try:
            page.goto(base, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            page.wait_for_timeout(500)

            field = browser_find_search_field(page)
            if field:
                variants = code_variants(code)
                for index, variant in enumerate(variants):
                    if expired():
                        return None
                    try:
                        # Re-open home between variants to avoid stale result states.
                        if index:
                            page.goto(base, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
                            page.wait_for_timeout(350)
                            field = browser_find_search_field(page)
                        if not field:
                            continue

                        field.fill(variant)
                        field.press("Enter")
                        page.wait_for_timeout(900)

                        # Some sites redirect directly to the product.
                        payload = rendered_product_payload(page, code, "browser - ricerca interna")
                        if payload:
                            cache_set(base, code, payload["url"], payload["title"])
                            return payload

                        # Otherwise inspect result cards.
                        for url in browser_candidate_links(page, base, code):
                            if expired():
                                return None
                            p = browser_open_verified(
                                ctx, url, base, code, "browser - ricerca interna"
                            )
                            if p:
                                return p
                    except Exception:
                        continue
        except Exception:
            pass
        finally:
            try:
                page.close()
            except Exception:
                pass

        # 3. Browser search engine fallback. Still verify by opening the real ecommerce page.
        page = ctx.new_page()
        try:
            query = quote_plus(f'site:{domain(base)} "{code}"')
            if expired():
                return None
            page.goto("https://www.bing.com/search?q="+query, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            page.wait_for_timeout(700)

            urls = []
            try:
                urls += page.locator("li.b_algo h2 a[href]").evaluate_all(
                    "els => els.map(a => a.href || '')"
                )
            except:
                pass
            if not urls:
                try:
                    urls += page.locator("a[href]").evaluate_all(
                        "els => els.slice(0,1200).map(a => a.href || '')"
                    )
                except:
                    pass

            for url in list(dict.fromkeys(urls))[:MAX_BING_RESULTS]:
                if expired():
                    return None
                if same_domain(url, base) and not is_home(url):
                    p = browser_open_verified(ctx, url, base, code, "browser · Bing")
                    if p:
                        return p
        except:
            pass
        finally:
            try:
                page.close()
            except:
                pass

        return None
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def browser_force_resolve(browser, base, code):
    """Fallback indipendente: prova URL di ricerca comuni e Bing RSS.

    Ogni candidato viene comunque aperto nel browser e accettato soltanto
    quando il codice prodotto e presente nel DOM renderizzato.
    """
    ctx = browser.new_context(
        locale="it-IT",
        viewport={"width":1365,"height":950},
        user_agent=HEADERS["User-Agent"]
    )
    seen = set()
    deadline = time.monotonic() + 25
    try:
        # Alcuni siti delegano la ricerca ad AccelaSearch. In headless il
        # widget puo non inizializzarsi, quindi interroghiamo la sua API e
        # verifichiamo comunque la scheda risultante nel browser reale.
        discovery = ctx.new_page()
        try:
            discovery.goto(base, wait_until="domcontentloaded", timeout=6000)
            discovery.wait_for_timeout(700)
            loaders = discovery.locator('script[src*="accelasearch.io"]').evaluate_all(
                "els => els.map(e => e.src || '')"
            )
        except Exception:
            loaders = []
        finally:
            discovery.close()
        for loader in loaders:
            match = re.match(r"(https://[^/]*accelasearch\.io/API/shops/[^/]+)/loader", loader, re.I)
            if not match:
                continue
            try:
                response = get(match.group(1)+"/search?"+urlencode({"q":code}))
                data = response.json()
                candidate = clean((data.get("header") or {}).get("urlPrefix"))
            except Exception:
                candidate = ""
            if candidate and same_domain(candidate, base):
                payload = browser_open_verified(ctx, candidate, base, code, "browser - AccelaSearch")
                if payload:
                    return payload

        # Prima usa la form di ricerca reale scoperta nell'HTML: e piu
        # rapida e precisa dei tentativi su percorsi generici.
        for variant in code_variants(code):
            if time.monotonic() >= deadline:
                return None
            for candidate in real_search_candidates(base, variant):
                if candidate in seen:
                    continue
                seen.add(candidate)
                payload = browser_open_verified(ctx, candidate, base, code, "browser - form sito")
                if payload:
                    return payload

        routes = (
            ("/search", "q"),
            ("/catalogsearch/result/", "q"),
            ("/cerca", "q"),
            ("/", "s"),
        )
        for variant in code_variants(code):
            for path, parameter in routes:
                if time.monotonic() >= deadline:
                    return None
                search_url = urljoin(base.rstrip("/")+"/", path.lstrip("/"))
                search_url += ("&" if "?" in search_url else "?") + urlencode({parameter: variant})
                page = ctx.new_page()
                try:
                    remaining = max(1000, int((deadline-time.monotonic())*1000))
                    page.goto(search_url, wait_until="domcontentloaded", timeout=min(6000, remaining))
                    page.wait_for_timeout(1600)
                    payload = rendered_product_payload(page, code, "browser - ricerca diretta")
                    if payload:
                        cache_set(base, code, payload["url"], payload["title"])
                        return payload
                    candidates = browser_candidate_links(page, base, code)
                except Exception:
                    candidates = []
                finally:
                    page.close()
                for candidate in candidates:
                    if candidate in seen:
                        continue
                    seen.add(candidate)
                    payload = browser_open_verified(ctx, candidate, base, code, "browser - ricerca diretta")
                    if payload:
                        return payload

        for candidate in bing_rss_candidates(base, code):
            if candidate in seen:
                continue
            seen.add(candidate)
            payload = browser_open_verified(ctx, candidate, base, code, "browser - Bing RSS")
            if payload:
                return payload
    finally:
        ctx.close()
    return None

# Normalizza e pulisce la stringa rimuovendo entità HTML, caratteri invisibili e convertendola in maiuscolo
def normalize_source(value):
    value = html.unescape(unquote_plus(str(value or "")))
    value = unicodedata.normalize("NFKC", value)

    # Rimuove caratteri invisibili che possono spezzare il codice
    value = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", value)

    return value.upper()

#cerca il codice anche se ci sono spazi, punti o trattini
def make_code_regex(code):
    compact = norm_code(normalize_source(code))
    if not compact:
        return None

    parts = re.findall(r"[A-Z]+|\d+", compact)
    if not parts:
        return None

    # Accetta spazi, trattini Unicode, punti, slash e altri separatori
    separator = r"[^A-Z0-9]{0,10}"
    pattern = separator.join(re.escape(part) for part in parts)

    return re.compile(
        rf"(?<![A-Z0-9]){pattern}(?![A-Z0-9])",
        re.IGNORECASE,
    )

# ---------------- PRICE EXTRACTION ----------------

#legge la pagina web ed estrae il testo di tutte le tabelle presenti
def table_rows(soup):
    out=[]
    for table in soup.find_all("table"):
        rows=[]
        for tr in table.find_all("tr"):
            cells=[clean(x.get_text(" ",strip=True)) for x in tr.find_all(["th","td"])]
            if cells:rows.append(cells)
        if rows:out.append((clean(table.get_text(" ",strip=True)),rows))
    return out

#analizza le tabelle riga per riga per associare i prezzi alle giuste quantità
def horizontal(rows,neutral_labels=(),printed_labels=()):
    neutral,printed={},{};qtys=None
    for row in rows[:10]:
        qs=[q for q in (qty_start(c) for c in row) if q is not None]
        if len(qs)>=2:qtys=qs;break
    if not qtys:return neutral,printed
    for row in rows:
        label=(row[0] if row else "").lower()
        vals=[parse_price(c) for c in row[1:]]
        vals=[v for v in vals if v is not None][:len(qtys)]
        if not vals:continue
        if any(x in label for x in neutral_labels):
            for q,p in zip(qtys,vals):neutral[str(q)]=p
        elif any(x in label for x in printed_labels):
            for q,p in zip(qtys,vals):printed[str(q)]=p
    return neutral,printed

#cerca ed estrae tuti i prezzi numerici presenti all'interno di un testo
def _price_list(s):
    return [parse_price(x) for x in re.findall(r"(\d+(?:[.,]\d{1,4}))", s or "")]

#cerca ed estrae tutti i numeri interi presenti in un testo
def _int_list(s):
    return [int(x) for x in re.findall(r"\b(\d{1,6})\b", s or "")]

#unisce la lista delle quantità ai rispettivi prezzi creando un dizionario
def _zip_prices(qtys, vals):
    return {str(int(q)): p for q, p in zip(qtys, vals) if p is not None}

#ritaglia una porzione specifica di testo compresa tra due parole chiave
def _chunk(text, start_label, end_labels=(), max_len=5000):
    low = text.lower()
    pos = low.find(start_label.lower())
    if pos < 0:
        return ""
    end = min(len(text), pos + max_len)
    for label in end_labels:
        p = low.find(label.lower(), pos + len(start_label))
        if p >= 0:
            end = min(end, p)
    return text[pos:end]

#estrae i prezzi neutri e personalizzati dal testo dei siti
def parse_generic(text):
    """
    Parser universale: estrae quantità e prezzi (neutri e personalizzati)
    da qualsiasi layout di e-commerce senza regole per singolo sito.
    """
    neutral, printed = {}, {}
    if not text:
        return neutral, printed

    #riconosce tabelle a 3 colonne: Quantità | Prezzo Neutro | Prezzo Personalizzato
    matches_dual = re.findall(
        r"(?<!\d)(\d{1,6})\s*(?:pz|pezzi)?\s*[:\-\|]?\s*€?\s*(\d+[.,]\d{1,4})\s*€?\s*(?:\||/|\s)\s*€?\s*(\d+[.,]\d{1,4})\s*€?",
        text, re.I
    )
    if matches_dual:
        for q, n, p in matches_dual:
            q_val = str(int(q))
            neutral[q_val] = parse_price(n)
            printed[q_val] = parse_price(p)
        return neutral, printed

    #se non trova la struttura unica, separa il testo tramite parole chiave di stampa
    sections = re.split(r"(?:con stampa|personalizza|sublimazione|serigrafia|incisione)", text, flags=re.I)
    
    text_neutral = sections[0]
    text_printed = sections[1] if len(sections) > 1 else ""

    def _extract_pairs(block):
        res = {}
        #estrae coppie: Quantità -> Prezzo
        pairs = re.findall(
            r"(?<!\d)(\d{1,6})(?:\s*-\s*\d+)?\s*(?:pz|pezzi)?\s*[:\-\|]?\s*€?\s*(\d+[.,]\d{1,4})\s*€?",
            block, re.I
        )
        for q, p in pairs:
            val = parse_price(p)
            if val is not None:
                res[str(int(q))] = val
        return res

    neutral = _extract_pairs(text_neutral)
    if text_printed:
        printed = _extract_pairs(text_printed)

    return neutral, printed

#cerca ed estrae i prezzi da tabelle sconosciute o non standard nel sito
def _merge_lowest(target, qtys, vals):
    for quantity, value in zip(qtys, vals):
        if value is None:
            continue
        key = str(int(quantity))
        if key not in target or value < target[key]:
            target[key] = value


def parse_unknown_table(soup):
    neutral, printed = {}, {}
    for table in soup.find_all("table"):
        ttext = clean(table.get_text(" ", strip=True))
        low = ttext.lower()
        if not any(x in low for x in ("quantità", "quantita", "quantity", "qty")):
            continue

        rows = []
        for tr in table.find_all("tr"):
            cells = [clean(x.get_text(" ", strip=True)) for x in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)

        qtys = None
        for row in rows[:8]:
            q = [qty_start(cell) for cell in row]
            q = [x for x in q if x is not None]
            if len(q) >= 2:
                qtys = q
                break
        if not qtys:
            continue

        table_neutral = any(x in low for x in (
            "senza stampa", "senza personalizzazione", "non include stampa",
            "unprinted", "without printing"
        ))
        table_printed = any(x in low for x in (
            "con stampa", "personalizz", "printed", "printing"
        ))

        for row in rows:
            if not row:
                continue
            label = row[0].lower()
            # Ignora intestazioni, righe verticali e duplicati responsive che
            # contengono l'intera tabella in una singola riga.
            if label.startswith(("quantit", "quantity", "qty")):
                continue
            if qty_start(row[0]) is not None:
                continue
            if len(row) > len(qtys) + 3:
                continue
            vals = [parse_price(c) for c in row[1:]]
            vals = [v for v in vals if v is not None][:len(qtys)]
            if len(vals) < 2:
                continue
            is_neutral = any(x in label for x in (
                "senza stampa", "senza personalizzazione", "unprinted",
                "without printing", "neutro"
            )) or (table_neutral and label.strip() in ("prezzo", "price"))
            is_printed = any(x in label for x in (
                "con stampa", "personalizz", "printed", "sublim",
                "quadricromia", "serigraf", "tampograf", "transfer",
                "ricamo", "incision", "colore"
            ))
            if is_neutral:
                _merge_lowest(neutral, qtys, vals)
            elif is_printed:
                _merge_lowest(printed, qtys, vals)
    return neutral, printed


def parse_range_price_ladder(text):
    """Legge listini del tipo 1-7, 8-23, 24-71 ... seguiti dai prezzi."""
    sequence_rx = re.compile(
        r"(?:(?<!\d)\d{1,6}\s*(?:-\s*\d{1,6}|\+)\s*){3,}",
        re.I
    )
    for match in sequence_rx.finditer(text or ""):
        quantities = [
            int(value) for value in re.findall(
                r"(?<!\d)(\d{1,6})\s*(?:-\s*\d{1,6}|\+)",
                match.group(0)
            )
        ]
        tail = (text or "")[match.end():match.end()+1000]
        prices = [
            parse_price(value) for value in re.findall(
                r"(?<!\d)(\d{1,5}[.,]\d{2,4})\s*(?:€|EUR)",
                tail,
                re.I
            )
        ]
        prices = [value for value in prices if value is not None]
        if len(quantities) >= 3 and len(prices) >= len(quantities):
            return _zip_prices(quantities, prices[:len(quantities)])
    return {}

#smista la pagina web al lettore del rispettivo sito per estrarre i prezzi corretti
def parse_prices(base, soup, text):
    neutral, printed=parse_unknown_table(soup)
    source="Tabella esplicita del sito"

    if not neutral and not printed:
        neutral = parse_range_price_ladder(text)
        if neutral:
            source = "Fasce quantita della pagina"
        else:
            neutral, printed = parse_generic(text)
            source = "Prezzi estratti dal testo della pagina"

    vat = (
        "IVA esclusa" if re.search(r"iva\s+esclusa|without vat", text, re.I)
        else "IVA inclusa" if re.search(r"iva\s+incl", text, re.I)
        else "IVA non verificata"
    )

    return {
        "neutral": neutral,
        "printed": printed,
        "generic": None,
        "generic_kind": None,
        "vat": vat,
        "price_source": source
    }

#confezziona il risultato finale combinando i dati del prodotto e i prezzi estratti
def make_result(site,base,product,code):
    name=clean(site.get("name")) or domain(base or "")
    result={"site":name,"base":base,"found":False,"product_name":None,"product_url":None,"resolver":None,
            "neutral":{},"printed":{},"generic":None,"generic_kind":None,"vat":"IVA non verificata",
            "verified_prices":False,"status":"Non trovato automaticamente","price_source":None,
            "match_score":0,"match_reasons":[]}
    if not product:return result
    prices=parse_prices(base,product["soup"],product["text"])
    result.update({"found":True,"product_name":product["title"],"product_url":product["url"],"resolver":product["source"],
                   "neutral":prices["neutral"],"printed":prices["printed"],"generic":prices["generic"],
                   "generic_kind":prices["generic_kind"],"vat":prices["vat"],"price_source":prices.get("price_source"),
                   "match_score":product.get("match_score",100),"match_reasons":product.get("match_reasons",["codice verificato nella pagina"])})
    result["verified_prices"]=bool(result["neutral"] or result["printed"] or result["generic"] is not None)
    result["status"]="Prezzi letti dalla pagina reale" if result["verified_prices"] else "Prodotto trovato; tabella prezzi non riconosciuta"
    return result

#trova la quantità migliore per confrontare i prezzi tra i veri siti web
def build_comparison(results):
    for kind in ("printed","neutral"):
        buckets={}
        for r in results:
            if r.get("vat") not in ("IVA esclusa","IVA inclusa"):continue
            for q,p in (r.get(kind) or {}).items():
                buckets.setdefault((int(q),r["vat"]),[]).append((r["site"],float(p)))
        valid=[(k,v) for k,v in buckets.items() if len(v)>=2]
        if not valid:continue
        valid.sort(key=lambda x:(-len(x[1]),x[0][0]))
        (q,vat),vals=valid[0];mn=min(p for _,p in vals);mx=max(p for _,p in vals)
        return {"kind":kind,"quantity":q,"vat":vat,"label":"Con stampa" if kind=="printed" else "Senza stampa",
                "cheapest_sites":[] if mn==mx else [s for s,p in vals if p==mn],
                "expensive_sites":[] if mn==mx else [s for s,p in vals if p==mx]}
    return None

#cicla tutti i siti web fornitori per cercare il prodotto ed estrarne i dati col browser
def run_sites(sites,code):
    """
    V15: browser-first. No product name is required from the user.
    A single Chromium instance is reused across all ecommerce sites.
    """
    resolved = []
    pw = browser = None
    try:
        pw, browser = open_browser()
        for site in sites:
            base = normalize_base(site.get("url"))
            product = None
            if base:
                try:
                    product = browser_resolve_exact(browser, base, code)
                    if not product:
                        product = browser_force_resolve(browser, base, code)
                except Exception as exc:
                    print(f"Ricerca fallita su {base}: {type(exc).__name__}: {exc}")
                    product = None
            resolved.append((base, product))
    except:
        # If Chromium cannot start, keep sites visible rather than invent results.
        resolved = [(normalize_base(site.get("url")), None) for site in sites]
    finally:
        try:
            if browser:
                browser.close()
        except:
            pass
        try:
            if pw:
                pw.stop()
        except:
            pass

    # La ricerca deve restituire i risultati anche se il database storico non
    # è temporaneamente raggiungibile.
    try:
        salva_prodotto(code, f"prodotto {code}")
    except Exception as exc:
        logger.warning("Prodotto non salvato nel database: %s", exc)

    #genera la lista dei risultati
    risultati=[
        make_result(sites[i], resolved[i][0], resolved[i][1], code)
        for i in range(len(sites))
    ]
    
    #salva ogni match nel database
    for res in risultati:
        if not res.get("found"):
            continue

        prezzi = list((res.get("neutral") or {}).values())
        prezzi += list((res.get("printed") or {}).values())

        prezzo = min(prezzi) if prezzi else res.get("generic")

        try:
            salva_match(
                codice_prodotto=code,
                nome_sito=res["site"],
                prezzo=prezzo,
                score=res.get("match_score", 0),
                url_prodotto=res.get("product_url", ""),
                url_base=res.get("base"),
            )
        except Exception as exc:
            logger.warning("Match di %s non salvato: %s", res["site"], exc)

    return risultati

@app.route("/automatico")
@login_required
def automatico():
    return render_template("automatico.html")

#riceve il codice prodotto e restituisce i risultati della ricerca automatica in JSON
@app.route("/manuale")
@login_required
def manuale():
    return redirect("/personalizzati")

#riceve il codice e la lista di siti dall'utente e restituisce i risultati in JSON 
@app.route("/api/automatico")
@login_required
def api_auto():
    code=clean(request.args.get("code"))
    if not code:return jsonify({"error":"Inserisci un codice valido"}),400
    results=run_sites(load_sites(),code)
    return jsonify({"code":code,"total_sites":len(results),"found_sites":sum(r["found"] for r in results),
                    "price_sites":sum(r["verified_prices"] for r in results),"comparison":build_comparison(results),"results":results})

#avvia ufficialmente l'applicazione web impostando l'indirizzo locale e la porta
@app.route("/api/manuale",methods=["POST"])
@login_required
@csrf_required
def api_manual():
    data=request.get_json(silent=True) or {};code=clean(data.get("code"));sites=data.get("sites") or []
    if not code:return jsonify({"error":"Inserisci un codice valido"}),400
    if not isinstance(sites, list):return jsonify({"error":"Lista siti non valida"}),400
    sites=[{"name":clean(x.get("name")),"url":clean(x.get("url"))} for x in sites[:10] if isinstance(x,dict) and clean(x.get("url"))]
    if not sites:return jsonify({"error":"Inserisci almeno un sito"}),400
    if any(not is_safe_public_url(site["url"]) for site in sites):
        return jsonify({"error":"Sono ammessi solo URL HTTP/HTTPS pubblici"}),400
    results=run_sites(sites,code)
    return jsonify({"code":code,"total_sites":len(results),"found_sites":sum(r["found"] for r in results),
                    "price_sites":sum(r["verified_prices"] for r in results),"comparison":build_comparison(results),"results":results})

if __name__=="__main__":
    print("PriceMatch V15: http://127.0.0.1:5000")
    app.run(host="127.0.0.1",port=5000,debug=False,threaded=True)
