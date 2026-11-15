/* Moteur « direct » : appelle PRIM et les données IDFM depuis le navigateur, sans serveur.
 * Port JavaScript de server.py — renvoie les mêmes formes JSON que les routes /api/*.
 * Utilisé quand la page est servie par un hébergement statique (GitHub Pages, appli Android installée).
 * La clé PRIM est stockée uniquement dans ce navigateur (localStorage). */
(function () {
  "use strict";

  const PRIM_STOP_MONITORING = "https://prim.iledefrance-mobilites.fr/marketplace/stop-monitoring";
  const PRIM_NAVITIA = "https://prim.iledefrance-mobilites.fr/marketplace/v2/navitia";
  const ODS_BASE = "https://data.iledefrance-mobilites.fr/api/explore/v2.1/catalog/datasets";
  const MODES = { Metro: "metro", RapidTransit: "rer", Tramway: "tram", Bus: "bus" };
  const SERIOUS_EFFECTS = new Set(["NO_SERVICE", "REDUCED_SERVICE", "SIGNIFICANT_DELAYS", "DETOUR", "STOP_MOVED", "MODIFIED_SERVICE"]);
  const KEY_STORE = "prim.apiKey";
  const CONFIG_STORE = "prim.config";
  const CACHE_PREFIX = "prim.cache:";

  /* ---------- stockage local (protégé : peut être indisponible) ---------- */
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); return true; } catch { return false; } },
    del(k) { try { localStorage.removeItem(k); } catch { /* rien */ } },
  };

  const getKey = () => (store.get(KEY_STORE) || "").trim();

  /* ---------- HTTP avec cache (mémoire, + localStorage pour les référentiels) ---------- */
  const memCache = new Map();

  class HttpError extends Error {
    constructor(status) { super("HTTP " + status); this.status = status; }
  }

  async function getJson(url, { headers = {}, ttl = 0, persist = false } = {}) {
    const now = Date.now();
    if (ttl) {
      const hit = memCache.get(url);
      if (hit && hit.exp > now) return hit.data;
      if (persist) {
        try {
          const raw = store.get(CACHE_PREFIX + url);
          if (raw) {
            const p = JSON.parse(raw);
            if (p.exp > now) { memCache.set(url, p); return p.data; }
          }
        } catch { /* cache corrompu : on ignore */ }
      }
    }
    const r = await fetch(url, { headers: { Accept: "application/json", ...headers } });
    if (!r.ok) throw new HttpError(r.status);
    const data = await r.json();
    if (ttl) {
      const entry = { exp: now + ttl * 1000, data };
      memCache.set(url, entry);
      if (persist && !store.set(CACHE_PREFIX + url, JSON.stringify(entry))) purgeCache();
    }
    return data;
  }

  function purgeCache() {
    try {
      Object.keys(localStorage).filter(k => k.startsWith(CACHE_PREFIX)).forEach(k => localStorage.removeItem(k));
    } catch { /* rien */ }
  }

  function primHeaders() {
    const key = getKey();
    if (!key) throw new Error("Clé API PRIM manquante : saisissez votre jeton dans les réglages (☰)");
    return { apikey: key };
  }

  const qs = (o) => new URLSearchParams(o).toString();

  /* ---------- utilitaires ---------- */
  function normalize(s) {
    return String(s || "").toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "")
      .replace(/[-']/g, " ").split(/\s+/).filter(Boolean).join(" ");
  }
  const stripCity = (n) => String(n || "").replace(/\s*\([^)]*\)$/, "").trim();

  function monitoringRef(stopId) {
    const sid = stopId.replace("IDFM:", "");
    if (sid.startsWith("monomodalStopPlace:")) return `STIF:StopArea:SP:${sid.split(":")[1]}:`;
    return `STIF:StopPoint:Q:${sid}:`;
  }
  const lineRef = (lineId) => `STIF:Line::${lineId.replace("IDFM:", "")}:`;
  const bareLine = (lineId) => lineId.replace("IDFM:", "");

  function siriValue(v) {
    if (Array.isArray(v)) v = v.length ? v[0] : null;
    if (v && typeof v === "object") return v.value ?? null;
    return v ?? null;
  }

  function stripHtml(t) {
    const withBreaks = String(t || "").replace(/<br\s*\/?>|<\/p>/gi, "\n");
    const text = new DOMParser().parseFromString(withBreaks, "text/html").body.textContent || "";
    return text.replace(/ /g, " ").split("\n").map(l => l.split(/\s+/).filter(Boolean).join(" "))
      .filter(Boolean).join("\n");
  }

  /* Heure de Paris au format Navitia (YYYYMMDDTHHMMSS) pour comparer avec application_periods */
  function parisNowNavitia() {
    const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/Paris", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }).formatToParts(new Date()).map(x => [x.type, x.value]));
    return `${p.year}${p.month}${p.day}T${p.hour}${p.minute}${p.second}`;
  }

  /* ---------- recherche d'arrêts ---------- */
  async function searchStops(q, mode) {
    q = String(q || "").replace(/"/g, " ").trim();
    if (q.length < 2) return [];
    const modes = MODES[mode] ? [mode] : Object.keys(MODES);
    const where = `search(stop_name, "${q}") AND mode IN (${modes.map(m => `"${m}"`).join(", ")})`;
    const rows = await getJson(`${ODS_BASE}/arrets-lignes/exports/json?` + qs({
      select: "id,shortname,route_long_name,stop_id,stop_name,mode,operatorname,nom_commune",
      where, limit: 1000,
    }), { ttl: 3600 });

    const groups = new Map();
    for (const r of rows) {
      const key = [r.stop_name, r.nom_commune, r.id].join("|");
      let g = groups.get(key);
      if (!g) {
        g = {
          stop_ids: [], stop_name: r.stop_name, city: r.nom_commune ?? null, line_id: r.id,
          line: r.shortname || r.route_long_name, mode: MODES[r.mode] || r.mode, operator: r.operatorname ?? null,
        };
        groups.set(key, g);
      }
      if (!g.stop_ids.includes(r.stop_id)) g.stop_ids.push(r.stop_id);
    }
    const out = [...groups.values()];
    const order = { metro: 0, rer: 1, tram: 2, bus: 3 };
    const nq = normalize(q);
    const relevance = (name) => {
      const n = normalize(name);
      if (n === nq) return 0;
      if (n.startsWith(nq)) return 1;
      if ((" " + n).includes(" " + nq)) return 2;
      return 3;
    };
    const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
    out.sort((a, b) =>
      relevance(a.stop_name) - relevance(b.stop_name)
      || (a.city !== "Paris") - (b.city !== "Paris")
      || cmp(a.stop_name, b.stop_name)
      || cmp(a.city || "", b.city || "")
      || (order[a.mode] ?? 9) - (order[b.mode] ?? 9)
      || cmp(String(a.line).padStart(4, "0"), String(b.line).padStart(4, "0")));
    const colors = await lineColors(out.map(o => o.line_id));
    out.forEach(o => Object.assign(o, colors[o.line_id] || {}));
    return out;
  }

  async function lineColors(lineIds) {
    const ids = [...new Set(lineIds.filter(Boolean).map(bareLine))].sort();
    const res = {};
    for (let i = 0; i < ids.length; i += 50) {
      const chunk = ids.slice(i, i + 50);
      const data = await getJson(`${ODS_BASE}/referentiel-des-lignes/records?` + qs({
        select: "id_line,shortname_line,colourweb_hexa,textcolourweb_hexa",
        where: `id_line IN (${chunk.map(c => `"${c}"`).join(", ")})`,
        limit: 100,
      }), { ttl: 86400, persist: true });
      for (const r of data.results || []) {
        res["IDFM:" + r.id_line] = {
          color: "#" + (r.colourweb_hexa || "888888"),
          text_color: "#" + (r.textcolourweb_hexa || "ffffff"),
        };
      }
    }
    return res;
  }

  /* ---------- sens de circulation ---------- */
  async function lineStops(lineId) {
    const rows = await getJson(`${ODS_BASE}/arrets-lignes/exports/json?` + qs({
      select: "stop_id,stop_name,stop_lat,stop_lon",
      where: `id = "IDFM:${bareLine(lineId)}"`,
      limit: 2000,
    }), { ttl: 86400, persist: true });
    const out = {};
    for (const r of rows) {
      const lon = parseFloat(r.stop_lon), lat = parseFloat(r.stop_lat);
      if (Number.isFinite(lon) && Number.isFinite(lat)) out[r.stop_id] = [lon, lat, r.stop_name];
    }
    return out;
  }

  function stopsByName(stops) {
    const o = {};
    for (const v of Object.values(stops)) o[normalize(stripCity(v[2]))] = v;
    return o;
  }

  const vector = (o, p) => [(p[0] - o[0]) * Math.cos(o[1] * Math.PI / 180), p[1] - o[1]];

  function originOf(stopIds, stops) {
    const pts = stopIds.filter(s => stops[s]).map(s => stops[s]);
    if (!pts.length) return null;
    return [pts.reduce((a, p) => a + p[0], 0) / pts.length, pts.reduce((a, p) => a + p[1], 0) / pts.length];
  }

  const navitia = (path, ttl) => getJson(`${PRIM_NAVITIA}/${path}`, { headers: primHeaders(), ttl, persist: true });

  async function quayDirections(stopId, lineId) {
    const lid = `line:IDFM:${bareLine(lineId)}`;
    const sp = `stop_point:IDFM:${stopId.replace("IDFM:", "")}`;
    let data;
    try { data = await navitia(`stop_points/${sp}/routes?depth=1&count=50`, 86400); } catch { return []; }
    const names = [];
    for (const r of data.routes || []) {
      const l = r.line && r.line.id;
      if (l && l !== lid) continue;
      const n = stripCity((r.direction && r.direction.name) || r.name);
      if (n && !names.includes(n)) names.push(n);
    }
    return names;
  }

  async function directions(stopIds, lineId) {
    const options = [{ key: "all", label: "Les deux sens" }];
    if (stopIds.length > 1) {
      const byLabel = new Map();
      for (const sid of stopIds) {
        let names = await quayDirections(sid, lineId);
        if (!names.length) {  // repli : destinations vues en temps réel sur ce quai
          try {
            const deps = (await departures([sid], lineId)).departures;
            names = [...new Set(deps.map(d => d.destination).filter(Boolean))].slice(0, 4);
          } catch { names = []; }
        }
        const label = names.join(", ") || `Quai ${sid.replace("IDFM:", "")}`;
        if (byLabel.has(label)) byLabel.get(label).stop_ids.push(sid);
        else byLabel.set(label, { key: "quay:" + sid, label, stop_ids: [sid] });
      }
      if (byLabel.size > 1) options.push(...byLabel.values());
      return options;
    }

    // quai unique (RER…) : regroupement géographique des terminus
    const stops = await lineStops(lineId);
    const origin = originOf(stopIds, stops);
    if (!origin) return options;
    let routes = [];
    try {
      routes = (await navitia(`lines/line:IDFM:${bareLine(lineId)}/routes?depth=2&count=100`, 86400)).routes || [];
    } catch { routes = []; }
    const termini = {};
    for (const r of routes) {
      const d = r.direction || {};
      const c = (d.stop_area && d.stop_area.coord) || {};
      const lon = parseFloat(c.lon), lat = parseFloat(c.lat);
      if (Number.isFinite(lon) && Number.isFinite(lat)) termini[stripCity(d.name)] = [lon, lat];
    }
    const vecs = {};
    for (const [n, p] of Object.entries(termini)) {
      const v = vector(origin, p);
      if (Math.hypot(v[0], v[1]) > 0.004) vecs[n] = v;  // ignore le terminus = cette gare
    }
    const names = Object.keys(vecs);
    if (names.length < 2) return options;
    // axe principal de la ligne autour de la gare (moyenne des angles doublés)
    let c = 0, s = 0;
    for (const v of Object.values(vecs)) {
      const a = Math.atan2(v[1], v[0]);
      c += Math.cos(2 * a); s += Math.sin(2 * a);
    }
    const th = Math.atan2(s, c) / 2;
    const axis = [Math.cos(th), Math.sin(th)];
    const sides = { 1: [], [-1]: [] };
    for (const [n, v] of Object.entries(vecs)) sides[v[0] * axis[0] + v[1] * axis[1] > 0 ? 1 : -1].push(n);
    for (const sign of [1, -1]) {
      if (!sides[sign].length) continue;
      options.push({
        key: `heading:${sign > 0 ? "+" : "-"}1:${th.toFixed(3)}`,
        label: sides[sign].sort().join(", "),
        heading: [round4(sign * axis[0]), round4(sign * axis[1])],
      });
    }
    return options;
  }
  const round4 = (x) => Math.round(x * 1e4) / 1e4;

  function destPoint(visit, stops, names) {
    const ref = visit.dest_ref || "";
    let m = ref.match(/STIF:StopArea:SP:(\d+):/);
    let sid = m ? `IDFM:monomodalStopPlace:${m[1]}` : null;
    if (!sid) { m = ref.match(/STIF:StopPoint:Q:(\d+):/); sid = m ? `IDFM:${m[1]}` : null; }
    if (sid && stops[sid]) return stops[sid];
    return names[normalize(stripCity(visit.destination || ""))] || null;
  }

  async function filterHeading(visits, stopIds, lineId, heading) {
    const stops = await lineStops(lineId);
    const origin = originOf(stopIds, stops);
    if (!origin) return visits;
    const names = stopsByName(stops);
    return visits.filter(v => {
      const p = destPoint(v, stops, names);
      if (!p) return true;  // destination inconnue : on préfère l'afficher
      const d = vector(origin, p);
      return d[0] * heading[0] + d[1] * heading[1] > 0;
    });
  }

  /* ---------- prochains passages ---------- */
  async function departures(stopIds, lineId, heading) {
    const headers = primHeaders();
    const deliveries = [];
    for (const stopId of stopIds) {
      const params = { MonitoringRef: monitoringRef(stopId) };
      if (lineId) params.LineRef = lineRef(lineId);
      // cache court pour ne pas épuiser le quota
      const data = await getJson(PRIM_STOP_MONITORING + "?" + qs(params), { headers, ttl: 20 });
      deliveries.push(...(((data.Siri || {}).ServiceDelivery || {}).StopMonitoringDelivery || []));
    }
    const result = parseDeliveries(deliveries);
    if (heading && lineId) result.departures = await filterHeading(result.departures, stopIds, lineId, heading);
    return result;
  }

  function parseDeliveries(deliveries) {
    const now = Date.now();
    const visits = [];
    for (const d of deliveries) {
      for (const v of d.MonitoredStopVisit || []) {
        const mvj = v.MonitoredVehicleJourney || {};
        const call = mvj.MonitoredCall || {};
        const expected = parseTime(call.ExpectedDepartureTime || call.ExpectedArrivalTime);
        const aimed = parseTime(call.AimedDepartureTime || call.AimedArrivalTime);
        const t = expected || aimed;
        if (!t) continue;
        const minutes = (t - now) / 60000;
        if (minutes < -1) continue;
        visits.push({
          line_ref: siriValue(mvj.LineRef),
          destination: siriValue(call.DestinationDisplay) || siriValue(mvj.DestinationName),
          direction: siriValue(mvj.DirectionName),
          dest_ref: siriValue(mvj.DestinationRef),
          mission: siriValue(mvj.JourneyNote),
          platform: siriValue(call.ArrivalPlatformName) || siriValue(call.DeparturePlatformName),
          time: t.toISOString(),
          minutes: Math.max(0, Math.floor(minutes)),
          realtime: !!expected,
          status: call.DepartureStatus || call.ArrivalStatus || null,
          at_stop: !!call.VehicleAtStop,
        });
      }
    }
    visits.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
    return { updated: new Date(now).toISOString(), departures: visits };
  }

  function parseTime(s) {
    if (!s) return null;
    const d = new Date(s);
    return isNaN(d) ? null : d;
  }

  /* ---------- perturbations ---------- */
  async function disruptions(lineId, stopName) {
    // 2 min de cache : l'info trafic évolue peu et le quota Navitia est plus serré
    const data = await getJson(`${PRIM_NAVITIA}/lines/line:IDFM:${bareLine(lineId)}/line_reports?count=100`,
      { headers: primHeaders(), ttl: 120 });
    const now = parisNowNavitia();
    const target = normalize(stopName || "");
    const out = [];
    const seen = new Set();
    for (const d of data.disruptions || []) {
      const periods = d.application_periods || [];
      if (periods.length && !periods.some(p => (p.begin || now) <= now && now <= (p.end || now))) continue;
      let scope = null;
      for (const o of d.impacted_objects || []) {
        const pt = o.pt_object || {};
        const kind = pt.embedded_type;
        if (["line", "network", "route", "line_section"].includes(kind)) { scope = "line"; break; }
        if ((kind === "stop_area" || kind === "stop_point") && target && normalize(stripCity(pt.name)) === target) scope = "stop";
      }
      if (!scope) continue;
      const msgs = {};
      for (const m of d.messages || []) {
        for (const t of (m.channel && m.channel.types) || []) if (!(t in msgs)) msgs[t] = stripHtml(m.text);
      }
      const text = msgs.web || msgs.email || msgs.sms || msgs.title || "";
      const title = msgs.title || text.split("\n")[0].slice(0, 90);
      const tags = (d.tags || []).map(t => String(t).toLowerCase());
      const effect = (d.severity || {}).effect || null;
      const context = [d.cause || "", d.category || "", title, ...tags].join(" ").toLowerCase();
      let kind;
      if (tags.includes("ascenseur")) kind = "elevator";
      else if (SERIOUS_EFFECTS.has(effect) && !context.includes("travaux")) kind = "serious";
      else kind = "info";  // travaux programmés, informations diverses
      // même alerte publiée pour chaque sens : on fusionne les textes sous un seul titre
      if (kind !== "elevator" && seen.has("t:" + title)) {
        const prev = out.find(x => x.title === title);
        if (prev && text && text !== title && !prev.text.includes(text)) prev.text = (prev.text + "\n\n" + text).trim();
        continue;
      }
      if (seen.has("tt:" + title + "\u0000" + text)) continue;
      seen.add("t:" + title);
      seen.add("tt:" + title + "\u0000" + text);
      out.push({
        id: d.id, title, text: text !== title ? text : "", effect, kind, scope,
        cause: d.cause || null, updated: d.updated_at || null,
      });
    }
    const order = { serious: 0, info: 1, elevator: 2 };
    out.sort((a, b) => order[a.kind] - order[b.kind] || (a.scope !== "stop") - (b.scope !== "stop"));
    return out;
  }

  /* ---------- configuration ---------- */
  function readConfig() {
    try { return JSON.parse(store.get(CONFIG_STORE)) || { stops: [] }; } catch { return { stops: [] }; }
  }
  function writeConfig(cfg) {
    if (!cfg || typeof cfg !== "object" || !Array.isArray(cfg.stops)) throw new Error("format de configuration invalide");
    if (!store.set(CONFIG_STORE, JSON.stringify(cfg))) throw new Error("Impossible d'enregistrer : stockage du navigateur indisponible");
  }

  /* ---------- routeur : mêmes chemins que server.py ---------- */
  function errorMessage(e) {
    if (e instanceof HttpError) {
      if (e.status === 401 || e.status === 403) return `Erreur API ${e.status} : clé API PRIM invalide ou non autorisée`;
      if (e.status === 429) return `Erreur API ${e.status} : quota dépassé`;
      return `Erreur API ${e.status}`;
    }
    if (e instanceof TypeError) return "Réseau indisponible";
    return e.message || String(e);
  }

  async function handle(path, opts = {}) {
    const u = new URL(path, location.href);
    const p = Object.fromEntries(u.searchParams);
    const method = (opts.method || "GET").toUpperCase();
    try {
      switch (u.pathname.replace(/^.*\/api\//, "/api/")) {
        case "/api/status": return { api_key: !!getKey(), mode: "direct" };
        case "/api/config":
          if (method === "PUT") { writeConfig(JSON.parse(opts.body)); return { ok: true }; }
          return readConfig();
        case "/api/search": return await searchStops(p.q || "", p.mode);
        case "/api/departures": {
          if (!p.stop) throw new Error("paramètre stop manquant");
          const stops = p.stop.split(",").filter(Boolean).slice(0, 6);
          const heading = p.heading ? p.heading.split(",").slice(0, 2).map(Number) : null;
          return await departures(stops, p.line || null, heading);
        }
        case "/api/directions":
          if (!p.stop || !p.line) throw new Error("paramètres stop et line requis");
          return await directions(p.stop.split(",").filter(Boolean).slice(0, 6), p.line);
        case "/api/disruptions":
          if (!p.line) throw new Error("paramètre line manquant");
          return await disruptions(p.line, p.stop || "");
        default: throw new Error("route inconnue : " + u.pathname);
      }
    } catch (e) {
      throw new Error(errorMessage(e));
    }
  }

  window.PrimDirect = {
    handle,
    getKey,
    setKey(k) { k = String(k || "").trim(); k ? store.set(KEY_STORE, k) : store.del(KEY_STORE); memCache.clear(); },
  };
})();
