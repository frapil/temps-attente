#!/usr/bin/env python3
"""Petit serveur local pour afficher les prochains passages RATP via l'API PRIM (IDFM).

- Sert la page web (static/index.html)
- Proxy vers l'API PRIM "Prochains passages" (stop-monitoring, SIRI Lite) en ajoutant la clé API
- Recherche d'arrêts dans le référentiel public "arrets-lignes" d'IDFM
- Sauvegarde la configuration des arrêts dans config.json

Aucune dépendance externe : Python 3.8+ suffit.
Clé API : variable d'environnement PRIM_API_KEY, ou fichier .env (PRIM_API_KEY=...).
"""
import html
import json
import math
import re
import shutil
import ssl
import subprocess
import os
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "docs")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
ENV_FILE = os.path.join(BASE_DIR, ".env")

PRIM_STOP_MONITORING = "https://prim.iledefrance-mobilites.fr/marketplace/stop-monitoring"
PRIM_NAVITIA = "https://prim.iledefrance-mobilites.fr/marketplace/v2/navitia"
ODS_BASE = "https://data.iledefrance-mobilites.fr/api/explore/v2.1/catalog/datasets"

# Modes du référentiel "arrets-lignes" proposés dans la recherche
MODES = {
    "Metro": "metro",
    "RapidTransit": "rer",
    "Tramway": "tram",
    "Bus": "bus",
}

_config_lock = threading.Lock()
_cache = {}  # url -> (expires_at, data)
_cache_lock = threading.Lock()


def load_api_key():
    key = os.environ.get("PRIM_API_KEY", "").strip()
    if key:
        return key
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("PRIM_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


API_KEY = load_api_key()


def curl_get_json(url, headers):
    # Les en-têtes (dont la clé API) passent par stdin pour ne pas apparaître dans la liste des processus
    cfg = "".join('header = "%s: %s"\n' % (k, v.replace('"', "")) for k, v in headers.items())
    p = subprocess.run(
        ["curl", "-sS", "--max-time", "15", "-K", "-", "-w", "\n%{http_code}", url],
        input=cfg.encode(), capture_output=True,
    )
    if p.returncode != 0:
        raise RuntimeError("Erreur réseau : %s" % p.stderr.decode(errors="replace").strip())
    body, _, code = p.stdout.decode("utf-8").rpartition("\n")
    code = int(code or 0)
    if code >= 400:
        raise urllib.error.HTTPError(url, code, body[:200], None, None)
    return json.loads(body)


def http_get_json(url, headers=None, ttl=0):
    """GET JSON avec petit cache mémoire (ttl secondes)."""
    now = time.time()
    if ttl:
        with _cache_lock:
            hit = _cache.get(url)
            if hit and hit[0] > now:
                return hit[1]
    headers = {"Accept": "application/json", **(headers or {})}
    if not ssl.HAS_TLSv1_3 and shutil.which("curl"):
        # Le Python fourni par macOS (LibreSSL 2.8) ne gère pas TLS 1.3, exigé par PRIM : on passe par curl
        data = curl_get_json(url, headers)
    else:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    if ttl:
        with _cache_lock:
            _cache[url] = (now + ttl, data)
    return data


# ---------------------------------------------------------------------------
# Conversions d'identifiants (référentiel IDFM -> SIRI)
# ---------------------------------------------------------------------------

def monitoring_ref(stop_id):
    """IDFM:22087 -> STIF:StopPoint:Q:22087:  /  IDFM:monomodalStopPlace:470195 -> STIF:StopArea:SP:470195:"""
    sid = stop_id.replace("IDFM:", "")
    if sid.startswith("monomodalStopPlace:"):
        return "STIF:StopArea:SP:%s:" % sid.split(":", 1)[1]
    return "STIF:StopPoint:Q:%s:" % sid


def line_ref(line_id):
    """IDFM:C01371 -> STIF:Line::C01371:"""
    return "STIF:Line::%s:" % line_id.replace("IDFM:", "")


def siri_value(v):
    """Les champs texte SIRI Lite sont soit {"value": "..."} soit une liste de ceux-ci."""
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        return v.get("value")
    return v


def parse_time(s):
    if not s:
        return None
    try:
        s = re.sub(r"\.\d+", "", s.strip()).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Fonctions métier
# ---------------------------------------------------------------------------

def normalize(s):
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.replace("-", " ").replace("'", " ").split())


def search_stops(q, mode=None):
    q = q.replace('"', " ").strip()
    if len(q) < 2:
        return []
    where = 'search(stop_name, "%s")' % q
    modes = [mode] if mode in MODES else list(MODES)
    where += " AND mode IN (%s)" % ", ".join('"%s"' % m for m in modes)
    params = urllib.parse.urlencode({
        "select": "id,shortname,route_long_name,stop_id,stop_name,mode,operatorname,nom_commune",
        "where": where,
        "limit": 1000,
    })
    # endpoint "exports" : pas de plafond à 100 résultats comme /records
    rows = http_get_json("%s/arrets-lignes/exports/json?%s" % (ODS_BASE, params), ttl=3600)
    # Une même ligne a souvent un arrêt (quai) par sens : on les regroupe
    groups = {}
    for r in rows:
        key = (r["stop_name"], r.get("nom_commune"), r["id"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "stop_ids": [],
                "stop_name": r["stop_name"],
                "city": r.get("nom_commune"),
                "line_id": r["id"],
                "line": r.get("shortname") or r.get("route_long_name"),
                "mode": MODES.get(r["mode"], r["mode"]),
                "operator": r.get("operatorname"),
            }
        if r["stop_id"] not in g["stop_ids"]:
            g["stop_ids"].append(r["stop_id"])
    out = list(groups.values())
    # regroupe : nom d'arrêt puis mode puis ligne
    order = {"metro": 0, "rer": 1, "tram": 2, "bus": 3}
    nq = normalize(q)

    def relevance(name):
        n = normalize(name)
        if n == nq:
            return 0
        if n.startswith(nq):
            return 1
        if (" " + nq) in (" " + n):
            return 2
        return 3

    out.sort(key=lambda x: (relevance(x["stop_name"]), x["city"] != "Paris", x["stop_name"], x["city"] or "",
                            order.get(x["mode"], 9), str(x["line"]).zfill(4)))
    lines = line_colors([o["line_id"] for o in out])
    for o in out:
        o.update(lines.get(o["line_id"], {}))
    return out


def line_colors(line_ids):
    ids = sorted({l.replace("IDFM:", "") for l in line_ids if l})
    if not ids:
        return {}
    res = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        params = urllib.parse.urlencode({
            "select": "id_line,shortname_line,colourweb_hexa,textcolourweb_hexa",
            "where": "id_line IN (%s)" % ", ".join('"%s"' % c for c in chunk),
            "limit": 100,
        })
        data = http_get_json("%s/referentiel-des-lignes/records?%s" % (ODS_BASE, params), ttl=86400)
        for r in data.get("results", []):
            res["IDFM:" + r["id_line"]] = {
                "color": "#" + (r.get("colourweb_hexa") or "888888"),
                "text_color": "#" + (r.get("textcolourweb_hexa") or "ffffff"),
            }
    return res


# ---------------------------------------------------------------------------
# Sens de circulation
#   - métro / tram / bus : chaque sens a son propre quai -> une carte = un quai
#   - RER (quai commun aux deux sens) : on trie les trains selon la position géographique
#     de leur terminus par rapport à la gare (côté "ouest" ou "est" de la ligne, par ex.)
# ---------------------------------------------------------------------------

def strip_city(name):
    return re.sub(r"\s*\([^)]*\)$", "", name or "").strip()


def line_stops(line_id):
    """Tous les arrêts d'une ligne (référentiel public) : {stop_id: (lon, lat, nom)}"""
    params = urllib.parse.urlencode({
        "select": "stop_id,stop_name,stop_lat,stop_lon",
        "where": 'id = "IDFM:%s"' % line_id.replace("IDFM:", ""),
        "limit": 2000,
    })
    rows = http_get_json("%s/arrets-lignes/exports/json?%s" % (ODS_BASE, params), ttl=86400)
    out = {}
    for r in rows:
        try:
            out[r["stop_id"]] = (float(r["stop_lon"]), float(r["stop_lat"]), r["stop_name"])
        except (TypeError, ValueError, KeyError):
            pass
    return out


def stops_by_name(stops):
    return {normalize(strip_city(v[2])): v for v in stops.values()}


def vector(origin, p):
    """Vecteur origine -> p, en degrés de latitude (longitude corrigée)."""
    return ((p[0] - origin[0]) * math.cos(math.radians(origin[1])), p[1] - origin[1])


def origin_of(stop_ids, stops):
    pts = [stops[sid] for sid in stop_ids if sid in stops]
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def navitia(path, ttl):
    return http_get_json("%s/%s" % (PRIM_NAVITIA, path), headers={"apikey": API_KEY}, ttl=ttl)


def quay_directions(stop_id, line_id):
    """Noms des terminus desservis depuis un quai (routes Navitia passant par ce quai)."""
    lid = "line:IDFM:%s" % line_id.replace("IDFM:", "")
    sp = "stop_point:IDFM:%s" % stop_id.replace("IDFM:", "")
    try:
        data = navitia("stop_points/%s/routes?depth=1&count=50" % sp, ttl=86400)
    except Exception:  # noqa: BLE001
        return []
    names = []
    for r in data.get("routes", []):
        if r.get("line", {}).get("id") not in (None, lid):
            continue
        n = strip_city(r.get("direction", {}).get("name") or r.get("name"))
        if n and n not in names:
            names.append(n)
    return names


def directions(stop_ids, line_id):
    """Sens proposés à la configuration d'une carte."""
    options = [{"key": "all", "label": "Les deux sens"}]
    if len(stop_ids) > 1:
        by_label = {}
        for sid in stop_ids:
            names = quay_directions(sid, line_id)
            if not names:  # repli : destinations vues en temps réel sur ce quai
                try:
                    deps = departures([sid], line_id)["departures"]
                    names = list(dict.fromkeys(d["destination"] for d in deps if d["destination"]))[:4]
                except Exception:  # noqa: BLE001
                    names = []
            label = ", ".join(names) or "Quai %s" % sid.replace("IDFM:", "")
            if label in by_label:
                by_label[label]["stop_ids"].append(sid)
            else:
                by_label[label] = {"key": "quay:" + sid, "label": label, "stop_ids": [sid]}
        if len(by_label) > 1:
            options += list(by_label.values())
        return options

    # quai unique (RER…) : regroupement géographique des terminus
    stops = line_stops(line_id)
    origin = origin_of(stop_ids, stops)
    if not origin:
        return options
    lid = "line:IDFM:%s" % line_id.replace("IDFM:", "")
    try:
        routes = navitia("lines/%s/routes?depth=2&count=100" % lid, ttl=86400).get("routes", [])
    except Exception:  # noqa: BLE001
        routes = []
    termini = {}
    for r in routes:
        d = r.get("direction", {})
        c = d.get("stop_area", {}).get("coord") or {}
        try:
            termini[strip_city(d.get("name"))] = (float(c["lon"]), float(c["lat"]))
        except (KeyError, TypeError, ValueError):
            pass
    vecs = {n: vector(origin, p) for n, p in termini.items()}
    vecs = {n: v for n, v in vecs.items() if math.hypot(*v) > 0.004}  # ignore le terminus = cette gare
    if len(vecs) < 2:
        return options
    # axe principal de la ligne autour de la gare (moyenne des angles doublés)
    c = sum(math.cos(2 * math.atan2(v[1], v[0])) for v in vecs.values())
    s_ = sum(math.sin(2 * math.atan2(v[1], v[0])) for v in vecs.values())
    th = math.atan2(s_, c) / 2
    axis = (math.cos(th), math.sin(th))
    sides = {1: [], -1: []}
    for n, v in vecs.items():
        sides[1 if v[0] * axis[0] + v[1] * axis[1] > 0 else -1].append(n)
    for sign in (1, -1):
        if sides[sign]:
            options.append({
                "key": "heading:%+d:%.3f" % (sign, th),
                "label": ", ".join(sorted(sides[sign])),
                "heading": [round(sign * axis[0], 4), round(sign * axis[1], 4)],
            })
    return options


def dest_point(visit, stops, names):
    ref = visit.get("dest_ref") or ""
    m = re.match(r"STIF:StopArea:SP:(\d+):", ref)
    sid = "IDFM:monomodalStopPlace:%s" % m.group(1) if m else None
    m = re.match(r"STIF:StopPoint:Q:(\d+):", ref)
    sid = sid or ("IDFM:%s" % m.group(1) if m else None)
    if sid and sid in stops:
        return stops[sid]
    n = normalize(strip_city(visit.get("destination") or ""))
    return names.get(n)


def filter_heading(visits, stop_ids, line_id, heading):
    stops = line_stops(line_id)
    origin = origin_of(stop_ids, stops)
    if not origin:
        return visits
    names = stops_by_name(stops)
    out = []
    for v in visits:
        p = dest_point(v, stops, names)
        if p is None:
            out.append(v)  # destination inconnue : on préfère l'afficher
            continue
        d = vector(origin, p)
        if d[0] * heading[0] + d[1] * heading[1] > 0:
            out.append(v)
    return out


def departures(stop_ids, line_id=None, heading=None):
    if not API_KEY:
        raise RuntimeError("Clé API PRIM manquante : ajoutez PRIM_API_KEY dans le fichier .env")
    deliveries = []
    for stop_id in stop_ids:
        params = {"MonitoringRef": monitoring_ref(stop_id)}
        if line_id:
            params["LineRef"] = line_ref(line_id)
        url = PRIM_STOP_MONITORING + "?" + urllib.parse.urlencode(params)
        # cache court pour ne pas épuiser le quota si plusieurs onglets sont ouverts
        data = http_get_json(url, headers={"apikey": API_KEY}, ttl=20)
        deliveries += data.get("Siri", {}).get("ServiceDelivery", {}).get("StopMonitoringDelivery", [])
    result = parse_deliveries(deliveries)
    if heading and line_id:
        result["departures"] = filter_heading(result["departures"], stop_ids, line_id, heading)
    return result


def parse_deliveries(deliveries):
    now = datetime.now(timezone.utc)
    visits = []
    for d in deliveries:
        for v in d.get("MonitoredStopVisit", []):
            mvj = v.get("MonitoredVehicleJourney", {})
            call = mvj.get("MonitoredCall", {})
            expected = parse_time(call.get("ExpectedDepartureTime") or call.get("ExpectedArrivalTime"))
            aimed = parse_time(call.get("AimedDepartureTime") or call.get("AimedArrivalTime"))
            t = expected or aimed
            if t is None:
                continue
            minutes = (t - now).total_seconds() / 60
            if minutes < -1:
                continue
            visits.append({
                "line_ref": siri_value(mvj.get("LineRef")),
                "destination": siri_value(call.get("DestinationDisplay")) or siri_value(mvj.get("DestinationName")),
                "direction": siri_value(mvj.get("DirectionName")),
                "dest_ref": siri_value(mvj.get("DestinationRef")),
                "mission": siri_value(mvj.get("JourneyNote")),
                "platform": siri_value(call.get("ArrivalPlatformName")) or siri_value(call.get("DeparturePlatformName")),
                "time": t.isoformat(),
                "minutes": max(0, int(minutes)),
                "realtime": expected is not None,
                "status": call.get("DepartureStatus") or call.get("ArrivalStatus"),
                "at_stop": bool(call.get("VehicleAtStop")),
            })
    visits.sort(key=lambda x: x["time"])
    return {"updated": now.isoformat(), "departures": visits}


def strip_html(t):
    t = re.sub(r"<br\s*/?>|</p>", "\n", t or "", flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t).replace("\xa0", " ")
    return "\n".join(" ".join(l.split()) for l in t.splitlines() if l.strip())


def navitia_time(s):
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%S")
    except (TypeError, ValueError):
        return None


SERIOUS_EFFECTS = {"NO_SERVICE", "REDUCED_SERVICE", "SIGNIFICANT_DELAYS", "DETOUR", "STOP_MOVED", "MODIFIED_SERVICE"}


def disruptions(line_id, stop_name=""):
    """Perturbations en cours (API Navitia de PRIM) concernant la ligne entière ou l'arrêt donné."""
    if not API_KEY:
        raise RuntimeError("Clé API PRIM manquante")
    url = "%s/lines/line:IDFM:%s/line_reports?count=100" % (PRIM_NAVITIA, line_id)
    # 2 min de cache : l'info trafic évolue peu et le quota Navitia est plus serré
    data = http_get_json(url, headers={"apikey": API_KEY}, ttl=120)

    now = datetime.now()  # les dates Navitia IDFM sont en heure de Paris
    target = normalize(stop_name)
    out, seen = [], set()
    for d in data.get("disruptions", []):
        periods = d.get("application_periods") or []
        if periods and not any(
            (navitia_time(p.get("begin")) or now) <= now <= (navitia_time(p.get("end")) or now) for p in periods
        ):
            continue
        scope = None
        for o in d.get("impacted_objects", []):
            pt = o.get("pt_object", {})
            kind = pt.get("embedded_type")
            if kind in ("line", "network", "route", "line_section"):
                scope = "line"
                break
            if kind in ("stop_area", "stop_point"):
                name = re.sub(r"\s*\([^)]*\)$", "", pt.get("name", ""))
                if target and normalize(name) == target:
                    scope = "stop"
        if not scope:
            continue
        msgs = {}
        for m in d.get("messages", []):
            for t in m.get("channel", {}).get("types", []):
                msgs.setdefault(t, strip_html(m.get("text")))
        text = msgs.get("web") or msgs.get("email") or msgs.get("sms") or msgs.get("title") or ""
        title = msgs.get("title") or text.split("\n")[0][:90]
        tags = [t.lower() for t in d.get("tags") or []]
        effect = d.get("severity", {}).get("effect")
        context = " ".join([d.get("cause") or "", d.get("category") or "", title] + tags).lower()
        if "ascenseur" in tags:
            kind = "elevator"
        elif effect in SERIOUS_EFFECTS and "travaux" not in context:
            kind = "serious"
        else:
            kind = "info"  # travaux programmés, informations diverses
        # même alerte publiée pour chaque sens : on fusionne les textes sous un seul titre
        if kind != "elevator" and title in seen:
            prev = next(x for x in out if x["title"] == title)
            if text and text != title and text not in prev["text"]:
                prev["text"] = (prev["text"] + "\n\n" + text).strip()
            continue
        if (title, text) in seen:
            continue
        seen.add(title)
        seen.add((title, text))
        out.append({
            "id": d.get("id"),
            "title": title,
            "text": text if text != title else "",
            "effect": effect,
            "kind": kind,
            "scope": scope,
            "cause": d.get("cause"),
            "updated": d.get("updated_at"),
        })
    order = {"serious": 0, "info": 1, "elevator": 2}
    out.sort(key=lambda x: (order[x["kind"]], x["scope"] != "stop"))
    return out


def read_config():
    with _config_lock:
        if not os.path.exists(CONFIG_FILE):
            return {"stops": []}
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)


def write_config(cfg):
    if not isinstance(cfg, dict) or not isinstance(cfg.get("stops"), list):
        raise ValueError("format de configuration invalide")
    with _config_lock:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)


# ---------------------------------------------------------------------------
# Serveur HTTP
# ---------------------------------------------------------------------------

class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                      ".webmanifest": "application/manifest+json", ".js": "text/javascript"}

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC_DIR, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/api/search":
                return self.send_json(search_stops(qs.get("q", ""), qs.get("mode")))
            if u.path == "/api/departures":
                if not qs.get("stop"):
                    return self.send_json({"error": "paramètre stop manquant"}, 400)
                stops = [x for x in qs["stop"].split(",") if x][:6]
                heading = None
                if qs.get("heading"):
                    heading = [float(x) for x in qs["heading"].split(",")[:2]]
                return self.send_json(departures(stops, qs.get("line"), heading))
            if u.path == "/api/directions":
                if not qs.get("stop") or not qs.get("line"):
                    return self.send_json({"error": "paramètres stop et line requis"}, 400)
                return self.send_json(directions([x for x in qs["stop"].split(",") if x][:6], qs["line"]))
            if u.path == "/api/disruptions":
                if not qs.get("line"):
                    return self.send_json({"error": "paramètre line manquant"}, 400)
                return self.send_json(disruptions(qs["line"].replace("IDFM:", ""), qs.get("stop", "")))
            if u.path == "/api/config":
                return self.send_json(read_config())
            if u.path == "/api/status":
                return self.send_json({"api_key": bool(API_KEY)})
        except urllib.error.HTTPError as e:
            msg = "Erreur API %s" % e.code
            if e.code in (401, 403):
                msg += " : clé API PRIM invalide ou non autorisée"
            elif e.code == 429:
                msg += " : quota dépassé"
            return self.send_json({"error": msg}, 502)
        except Exception as e:  # noqa: BLE001
            return self.send_json({"error": str(e)}, 500)
        return super().do_GET()

    def do_PUT(self):
        if urllib.parse.urlparse(self.path).path != "/api/config":
            return self.send_json({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            write_config(json.loads(self.rfile.read(n).decode("utf-8")))
            return self.send_json({"ok": True})
        except Exception as e:  # noqa: BLE001
            return self.send_json({"error": str(e)}, 400)


def main():
    port = int(os.environ.get("PORT", "8000"))
    if not API_KEY:
        print("⚠️  Aucune clé PRIM_API_KEY trouvée (.env ou variable d'environnement).")
        print("   La recherche d'arrêts fonctionne, mais pas les horaires temps réel.")
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        if e.errno == 48 or "in use" in str(e):
            print("Le port %d est déjà utilisé : le serveur tourne sans doute déjà → http://localhost:%d" % (port, port))
            print("Pour l'arrêter :  pkill -f 'server.py'   — ou lancez sur un autre port :  PORT=8001 python3 server.py")
            sys.exit(1)
        raise
    print("Serveur démarré : http://localhost:%d  (Ctrl+C pour arrêter)" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
