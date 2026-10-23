#!/usr/bin/env python3
"""Genera il sito della Rete UdR 5 dal GTFS.

Uso:
    python3 sito-mappe/build_data.py [percorso_gtfs.zip]

Senza argomento usa tutti gli zip GTFS nella radice del repo. Ogni GTFS vale dalla data
di inizio del suo calendario (start_date più bassa di calendar.txt) fino al giorno prima
dell'inizio del GTFS successivo: così un nuovo GTFS si può caricare subito e le sue
modifiche si vedono solo scegliendo un giorno dalla sua data di validità in poi.
A parità di data di inizio vale lo zip caricato per ultimo (secondo git, altrimenti per data file).

Riempie sito-mappe/template.html con i dati e scrive due copie identiche del sito,
un unico file HTML autonomo: sito-mappe/index.html (pubblicato online) e index.html
nella radice del repo (da aprire con un doppio clic).
"""
import base64
import csv
import datetime
import glob
import io
import json
import math
import os
import re
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
TITOLO = "Rete bus UdR 5"

# calendari: si usano calendar.txt e calendar_dates.txt del GTFS (eccezioni comprese);
# qui solo le etichette da mostrare. Un service_id non elencato prende i giorni della settimana
# ("Lunedì–Venerdì", "Sabato", …).
ETICHETTE = {
    "L1_SCLFER": "Scolastico feriale",
    "L1_LCF": "Feriale lunedì–sabato",
    "L2_SCLPREF": "Scolastico sabato",
    "L3_SCLFEST": "Scolastico festivo",
}

# località scritte in testa al nome fermata ("GUIDONIA | Via Roma # f11738") con la grafia estesa;
# le altre diventano "Iniziali Maiuscole" (TIVOLI -> Tivoli)
LOCALITA = {
    "GUIDONIA": "Guidonia Montecelio", "SAN POLO": "San Polo dei Cavalieri", "CERRETO": "Cerreto Laziale",
    "ANTICOLI": "Anticoli Corrado", "ARCINAZZO": "Arcinazzo Romano", "VIVARO": "Vivaro Romano",
    "SANT'ANGELO": "Sant'Angelo Romano", "SAN VITO": "San Vito Romano", "BIVIO ROVIANO": "Roviano",
    "BIVIO RIOFREDDO": "Riofreddo",
}
NOMI_COMUNI = {
    "Guidonia Montecelio", "Tivoli", "Subiaco", "Castel Madama", "Ciciliano", "Marcellina", "San Polo dei Cavalieri",
    "Fonte Nuova", "Cerreto Laziale", "Pisoniano", "Arsoli", "Gerano", "Rocca Canterano", "Jenne", "Riofreddo",
    "Vicovaro", "Canterano", "Affile", "Anticoli Corrado", "Arcinazzo Romano", "Roviano", "Vallinfreda",
    "Sant'Angelo Romano", "Sambuci", "Vallepietra", "Vivaro Romano", "Saracinesco", "San Vito Romano", "Roma",
    "Mandela", "Agosta", "Sant'Angelo Romano",
}
# frazioni (scritte negli indirizzi delle fermate) -> comune
FRAZIONI = {
    "Tivoli Terme": "Tivoli", "Villa Adriana": "Tivoli", "Santa Balbina": "Tivoli", "Arci-empolitana": "Tivoli",
    "Ponte Lucidi-la Torre": "Tivoli", "Villanova": "Guidonia Montecelio", "Colleverde": "Guidonia Montecelio",
    "Setteville": "Guidonia Montecelio", "Madonna della Pace": "Agosta", "Madonna Della Pace": "Agosta",
}
COMUNE_MAX_KM = 3       # fermata senza comune nel nome: comune della fermata più vicina entro 3 km
PRINC_OGNI_MIN = 5      # almeno una fermata principale ogni 5 minuti di viaggio (se il GTFS non ha timepoint)
PRINC_NODO_LINEE = 4    # nodo di interscambio: fermata servita da almeno 4 altre linee

GIORNI = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]


def find_gtfs():
    """Zip GTFS della radice del repo -> [(data_inizio, percorso)] ordinati per data, uno per data."""
    zips = glob.glob(os.path.join(ROOT, "*.zip"))
    if not zips:
        raise SystemExit("Nessun file .zip GTFS trovato nella radice del repo.")

    def committed(path):
        try:
            out = subprocess.run(["git", "log", "-1", "--format=%ct", "--", os.path.basename(path)],
                                 cwd=ROOT, capture_output=True, text=True).stdout.strip()
            return int(out) if out else float("inf")  # non ancora in git: è il più recente
        except OSError:
            return 0

    def calendar_range(path):
        try:
            zf = zipfile.ZipFile(path)
            cal = read(zf, "calendar.txt")
            starts = [c["start_date"] for c in cal] or [c["date"] for c in read(zf, "calendar_dates.txt")]
            ends = [c["end_date"] for c in cal] + [c["date"] for c in read(zf, "calendar_dates.txt")]
            return (min(starts), max(ends)) if starts else None
        except (zipfile.BadZipFile, KeyError):
            return None

    per_data = {}
    for z in zips:
        rng = calendar_range(z)
        if not rng:
            print(f"Ignorato (non è un GTFS con calendario): {os.path.basename(z)}")
            continue
        # a parità di data di inizio vince lo zip caricato per ultimo, poi quello che finisce più tardi
        key = (committed(z), rng[1], os.path.getmtime(z))
        if rng[0] not in per_data or key > per_data[rng[0]][0]:
            per_data[rng[0]] = (key, z)
    if not per_data:
        raise SystemExit("Nessun GTFS valido nella radice del repo.")
    return sorted((d, z) for d, (_, z) in per_data.items())


def read(zf, name):
    if name not in zf.namelist():
        return []
    with zf.open(name) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))


def natural(code):
    """'1D' -> (1, 'D'): ordina 1, 1D, 2, … 10 invece di 1, 10, 1D, 2."""
    m = re.match(r"(\d*)(.*)", code)
    return (int(m.group(1)) if m.group(1) else 10**6, m.group(2))


RE_INDIRIZZO = re.compile(r",?\s*\d{5}\s+(.+?)\s+[A-Z]{2},\s*Italia\s*$")


def split_stop_name(name):
    """'GUIDONIA | Via Maremmana Via Campanella # f12245' -> ('Via Maremmana Via Campanella', 'Guidonia Montecelio');
    'Via Dante Alighieri, 4, 00011 Tivoli Terme RM, Italia' -> ('Via Dante Alighieri, 4', 'Tivoli Terme')."""
    name = re.sub(r"\s+", " ", name.strip())
    loc = ""
    m = RE_INDIRIZZO.search(name)
    if m:
        loc = m.group(1)
        name = name[: m.start()]
    m = re.match(r"^([A-Za-z][A-Za-z' .]+?)\s*\|\s*(.+)$", name)
    if m:
        key = m.group(1).strip().upper()
        loc = loc or LOCALITA.get(key, m.group(1).strip().title())
        name = m.group(2)
    name = re.sub(r"\s*#\s*f\d+\s*", " ", name).strip(" ,")
    if not loc and name.title() in NOMI_COMUNI:
        loc = name.title()
    return (name[:1].upper() + name[1:], loc)


def comune_of(loc):
    """Località della fermata -> comune ('' se non noto)."""
    if loc in NOMI_COMUNI:
        return loc
    return FRAZIONI.get(loc, "")


def stop_comuni(stops):
    """stop_id -> comune; le fermate senza comune nel nome prendono quello della fermata più vicina."""
    out, noti = {}, []
    for sid, s in stops.items():
        com = comune_of(split_stop_name(s["stop_name"])[1])
        out[sid] = com
        if com:
            noti.append((float(s["stop_lat"]), float(s["stop_lon"]), com))
    for sid, s in stops.items():
        if out[sid] or not noti:
            continue
        la, lo = float(s["stop_lat"]), float(s["stop_lon"])
        kx = 111.32 * math.cos(math.radians(la))
        d, com = min((math.hypot((b - lo) * kx, (a - la) * 110.54), c) for a, b, c in noti)
        out[sid] = com if d <= COMUNE_MAX_KM else ""
    return out


def minutes(t):
    # orari al minuto (hh:mm): i secondi si troncano, 06:54:30 -> 06:54
    h, m, _ = (int(x) for x in t.split(":"))
    return h * 60 + m


def encode_polyline(points):
    out, plat, plon = [], 0, 0
    for lat, lon in points:
        ilat, ilon = round(lat * 1e5), round(lon * 1e5)
        for v in (ilat - plat, ilon - plon):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1f)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = ilat, ilon
    return "".join(out)


def simplify(points, tol_m=5.0):
    """Douglas-Peucker in metri."""
    if len(points) < 3:
        return points
    kx = 111320 * math.cos(math.radians(points[0][0]))
    xy = [(p[1] * kx, p[0] * 110540) for p in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = xy[a], xy[b]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            t = 0 if L2 == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L2))
            d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > best:
                best, idx = d, i
        if best > tol_m:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(points, keep) if k]


def line_colors(n):
    # colori distinti, abbastanza scuri da reggere il testo bianco del riquadro linea
    def hsl(h, s, l):
        s, l = s / 100, l / 100
        c = (1 - abs(2 * l - 1)) * s
        x = c * (1 - abs((h / 60) % 2 - 1))
        m = l - c / 2
        r, g, b = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][int(h // 60) % 6]
        return "#%02X%02X%02X" % tuple(round((v + m) * 255) for v in (r, g, b))
    return [hsl((i * 137.508) % 360, 70, 40) for i in range(n)]


def service_label(weekdays):
    """Insieme di giorni della settimana -> 'Lunedì–Venerdì', 'Sabato', 'Domenica e festivi', …"""
    ds = sorted(weekdays)
    if not ds:
        return "Nessun giorno"
    if ds == [6]:
        return "Domenica e festivi"
    runs, start = [], ds[0]
    for a, b in zip(ds, ds[1:] + [None]):
        if b != a + 1:
            runs.append((start, a))
            start = b
    parts = [GIORNI[a] if a == b else (f"{GIORNI[a]} e {GIORNI[b]}" if b == a + 1 else f"{GIORNI[a]}–{GIORNI[b]}")
             for a, b in runs]
    return ", ".join(parts)


def ymd(d):
    return d.strftime("%Y%m%d")


def build(zpath, inizio="00000000", fine="99999999"):
    """Dati del sito per un GTFS, con i calendari limitati al periodo inizio–fine (inclusi)."""
    zf = zipfile.ZipFile(zpath)
    agency = (read(zf, "agency.txt") or [{}])[0]
    routes = read(zf, "routes.txt")
    stops = {s["stop_id"]: s for s in read(zf, "stops.txt")}
    trips = read(zf, "trips.txt")

    st_by_trip = defaultdict(list)
    for r in read(zf, "stop_times.txt"):
        st_by_trip[r["trip_id"]].append(r)
    for v in st_by_trip.values():
        v.sort(key=lambda r: int(r["stop_sequence"]))
    trips = [t for t in trips if st_by_trip.get(t["trip_id"])]

    # ---- calendari dal GTFS (calendar.txt + eccezioni di calendar_dates.txt)
    eccezioni = defaultdict(dict)
    for e in read(zf, "calendar_dates.txt"):
        eccezioni[e["service_id"]][e["date"]] = e["exception_type"]
    nomi_giorni = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    cal = {c["service_id"]: c for c in read(zf, "calendar.txt")}
    servizi = {}
    for svc in sorted(set(cal) | set(eccezioni)):
        c = cal.get(svc)
        wd = {i for i, g in enumerate(nomi_giorni) if c and c[g] == "1"}
        ds = set()
        if c:
            d = datetime.datetime.strptime(c["start_date"], "%Y%m%d").date()
            end = datetime.datetime.strptime(c["end_date"], "%Y%m%d").date()
            while d <= end:
                if d.weekday() in wd:
                    ds.add(ymd(d))
                d += datetime.timedelta(days=1)
        for g, tipo in eccezioni[svc].items():
            if tipo == "1":
                ds.add(g)
            else:
                ds.discard(g)
        ds = sorted(g for g in ds if inizio <= g <= fine)
        if not ds:
            continue
        esclusi = sorted(g for g, tipo in eccezioni[svc].items() if tipo == "2" and inizio <= g <= fine)
        servizi[svc] = {"label": ETICHETTE.get(svc) or service_label(wd), "dal": ds[0], "al": ds[-1], "esclusi": esclusi,
                        "date": ds, "giorni": len(ds), "scol": "SCL" in svc}
    ordine = list(ETICHETTE)
    used_svc = sorted((s for s in servizi if any(t["service_id"] == s for t in trips)),
                      key=lambda s: (ordine.index(s) if s in ordine else len(ordine), s))
    servizi = {s: servizi[s] for s in used_svc}
    labels = Counter(v["label"] for v in servizi.values())
    for s, v in servizi.items():  # due calendari con la stessa etichetta: si distinguono col codice
        if labels[v["label"]] > 1:
            v["label"] += f" ({s})"
    trips = [t for t in trips if t["service_id"] in servizi]
    if not trips:
        return None

    # ---- fermate usate
    comuni = stop_comuni(stops)
    used_stops = sorted({r["stop_id"] for t in trips for r in st_by_trip[t["trip_id"]]},
                        key=lambda sid: (*split_stop_name(stops[sid]["stop_name"])[::-1], sid))
    stop_idx = {sid: i for i, sid in enumerate(used_stops)}
    fermate = []
    for sid in used_stops:
        s = stops[sid]
        nome, loc = split_stop_name(s["stop_name"])
        loc = loc or comuni[sid]  # nome senza località: comune della fermata più vicina
        fermate.append([s.get("stop_code") or sid, nome, loc, round(float(s["stop_lat"]), 6), round(float(s["stop_lon"]), 6)])

    # ---- percorsi
    pts = defaultdict(list)
    for r in read(zf, "shapes.txt"):
        pts[r["shape_id"]].append((int(r["shape_pt_sequence"]), float(r["shape_pt_lat"]), float(r["shape_pt_lon"]),
                                   float(r.get("shape_dist_traveled") or 0)))
    shapes, shape_km = {}, {}
    for sid in sorted({t["shape_id"] for t in trips}):
        seq = sorted(pts.get(sid, []))
        line = [(p[1], p[2]) for p in seq]
        km = 0
        if len(line) > 1:  # lunghezza geometrica (shape_dist_traveled del GTFS è in km, non in metri)
            kx = 111.32 * math.cos(math.radians(line[0][0]))
            km = sum(math.hypot((b[1] - a[1]) * kx, (b[0] - a[0]) * 110.54) for a, b in zip(line, line[1:]))
        shapes[sid] = encode_polyline(simplify(line))
        shape_km[sid] = round(km, 1)

    timepoint = any(r.get("timepoint") == "0" for t in trips for r in st_by_trip[t["trip_id"]])

    # ---- linee
    route_lines = defaultdict(set)  # fermata -> linee che la servono (per le fermate di interscambio)
    for t in trips:
        for r in st_by_trip[t["trip_id"]]:
            route_lines[r["stop_id"]].add(t["route_id"])

    # comune principale di ogni linea (quello con più passaggi) e comuni serviti
    passaggi = defaultdict(Counter)
    for t in trips:
        for r in st_by_trip[t["trip_id"]]:
            com = comuni[r["stop_id"]]
            if com:
                passaggi[t["route_id"]][com] += 1
    area = {rid: c.most_common(1)[0][0] for rid, c in passaggi.items()}
    routes = sorted(routes, key=lambda r: natural(r["route_short_name"]))
    colors = line_colors(len(routes))
    linee = []
    for ri, r in enumerate(routes):
        rtrips = [t for t in trips if t["route_id"] == r["route_id"]]
        if not rtrips:
            continue
        tabs = []
        groups = defaultdict(list)
        for t in rtrips:
            groups[(t["service_id"], t["direction_id"] or "0")].append(t)
        for (svc, d), ts in sorted(groups.items(), key=lambda kv: (used_svc.index(kv[0][0]), kv[0][1])):
            tabs.append(make_tab(svc, d, ts, st_by_trip, stops, stop_idx, route_lines, comuni, timepoint))
        color = (r.get("route_color") or "").strip()
        shp = sorted({t["shape_id"] for t in rtrips})
        linee.append({
            "id": r["route_short_name"], "n": r["route_short_name"], "nome": r["route_long_name"] or r["route_short_name"],
            "area": area.get(r["route_id"], ""), "comuni": sorted(passaggi[r["route_id"]]), "col": "#" + color.upper() if color else colors[ri],
            "shapes": shp, "km": max(shape_km[s] for s in shp), "tab": tabs,
        })

    linee.sort(key=lambda l: (l["area"], natural(l["n"])))
    all_dates = [d for v in servizi.values() for d in v["date"]]
    return {
        "agenzia": {"nome": agency.get("agency_name", ""), "url": agency.get("agency_url", ""),
                    "tel": agency.get("agency_phone", "")},
        # nel sito pubblicato non compaiono il nome del file GTFS né dettagli tecnici
        "periodo": {"dal": min(all_dates), "al": max(all_dates)},
        "servizi": servizi, "fermate": fermate, "shapes": shapes, "shape_km": shape_km, "linee": linee,
    }


def merge_seq(base, seq):
    """Supersequenza comune di `base` e `seq` (tramite LCS): entrambe ne restano sottosequenze."""
    n, m = len(base), len(seq)
    lcs = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            lcs[i][j] = lcs[i + 1][j + 1] + 1 if base[i] == seq[j] else max(lcs[i + 1][j], lcs[i][j + 1])
    out, i, j = [], 0, 0
    while i < n and j < m:
        if base[i] == seq[j]:
            out.append(base[i]); i += 1; j += 1
        elif lcs[i + 1][j] >= lcs[i][j + 1]:
            out.append(base[i]); i += 1
        else:
            out.append(seq[j]); j += 1
    return out + base[i:] + seq[j:]


def embed(rows, seq):
    """Righe del quadro occupate da `seq` (sottosequenza di `rows`), da sinistra: vale anche per le
    circolari che passano due volte dalla stessa fermata."""
    out, j = [], 0
    for x in seq:
        while rows[j] != x:
            j += 1
        out.append(j)
        j += 1
    return out


def make_tab(svc, d, ts, st_by_trip, stops, stop_idx, route_lines, comuni, timepoint):
    """Quadro orario di una direzione: righe = unione ordinata delle fermate di tutte le corse."""
    ts = sorted(ts, key=lambda t: minutes(st_by_trip[t["trip_id"]][0]["departure_time"]))
    ks = [tuple(r["stop_id"] for r in st_by_trip[t["trip_id"]]) for t in ts]
    # si parte dal percorso più frequente (a parità, il più lungo) e si aggiungono gli altri
    freq = Counter(ks)
    patterns = sorted(freq, key=lambda k: (-freq[k], -len(k), k))
    rows = list(patterns[0])
    for k in patterns[1:]:
        rows = merge_seq(rows, list(k))
    posizioni = {k: embed(rows, k) for k in patterns}

    corse = []
    princ = [0] * len(rows)
    for t, k in zip(ts, ks):
        o = [None] * len(rows)
        for i, r in zip(posizioni[k], st_by_trip[t["trip_id"]]):
            o[i] = minutes(r["departure_time"] or r["arrival_time"])
            if timepoint and r.get("timepoint") == "1":
                princ[i] = 1
        corse.append({"id": t.get("trip_short_name") or t["trip_id"], "shape": t["shape_id"], "o": o})

    # fermate principali: quelle con orario garantito del GTFS (timepoint = 1); se il GTFS non le
    # distingue, la prima fermata in ogni comune, i nodi di interscambio e una ogni PRINC_OGNI_MIN minuti
    for c in corse:  # capolinea di ogni corsa
        served = [i for i, v in enumerate(c["o"]) if v is not None]
        princ[served[0]] = princ[served[-1]] = 1
    if not timepoint:
        route_id = ts[0]["route_id"]
        last_com = None
        for i, sid in enumerate(rows):
            com = comuni.get(sid, "")
            if com:
                if last_com and com != last_com:  # ingresso in un altro comune
                    princ[i] = 1
                last_com = com
            if len(route_lines[sid] - {route_id}) >= PRINC_NODO_LINEE:
                princ[i] = 1
        longest = max(corse, key=lambda c: sum(v is not None for v in c["o"]))
        last_t = None
        for i, v in enumerate(longest["o"]):
            if v is None:
                continue
            if princ[i] or last_t is None or v - last_t >= PRINC_OGNI_MIN:
                princ[i] = 1
                last_t = v

    heads = Counter(t["trip_headsign"].strip() for t in ts if t["trip_headsign"].strip())
    last = split_stop_name(stops[rows[-1]]["stop_name"])[0]
    titolo = heads.most_common(1)[0][0] if heads else last
    titolo = re.sub(r"\s*\((Andata|Ritorno)\)\s*$", "", titolo)
    return {"svc": svc, "dir": d, "titolo": titolo, "fermate": [stop_idx[sid] for sid in rows],
            "princ": princ, "corse": corse}


def main():
    if len(sys.argv) > 1:
        versioni = [("00000000", sys.argv[1])]
    else:
        versioni = find_gtfs()
    dati = []
    for i, (inizio, zpath) in enumerate(versioni):
        fine = "99999999"
        if i + 1 < len(versioni):
            fine = ymd(datetime.datetime.strptime(versioni[i + 1][0], "%Y%m%d").date() - datetime.timedelta(days=1))
        data = build(zpath, inizio, fine)
        if not data:
            print(f"GTFS: {os.path.basename(zpath)} — nessun giorno di servizio nel periodo, ignorato")
            continue
        data["file"] = os.path.basename(zpath)  # solo per il messaggio qui sotto, tolto prima di scrivere il sito
        dati.append(data)
    if not dati:
        raise SystemExit("Nessun GTFS con giorni di servizio.")
    # ogni versione copre i giorni fino all'inizio della successiva (domeniche comprese)
    for a, b in zip(dati, dati[1:]):
        a["periodo"]["al"] = ymd(datetime.datetime.strptime(b["periodo"]["dal"], "%Y%m%d").date() - datetime.timedelta(days=1))
    feed = {"dal": dati[0]["periodo"]["dal"], "al": dati[-1]["periodo"]["al"]}
    for d in dati:
        d["feed"] = feed
        n = sum(len(t["corse"]) for l in d["linee"] for t in l["tab"])
        print(f"GTFS: {d.pop('file')} — in vigore dal {d['periodo']['dal']} al {d['periodo']['al']}: "
              f"{len(d['linee'])} linee, {len(d['fermate'])} fermate, {n} corse")

    with open(os.path.join(HERE, "template.html"), encoding="utf-8") as f:
        html = f.read()
    blob = json.dumps(dati, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    with open(os.path.join(HERE, "logo.png"), "rb") as f:  # logo incluso nella pagina: online va solo index.html
        logo = "data:image/png;base64," + base64.b64encode(f.read()).decode()
    for old, new in (("/*DATI*/null", blob), ("/*TITOLO*/", TITOLO), ("/*LOGO*/", logo)):
        if old not in html:
            raise SystemExit(f"Segnaposto {old} non trovato in template.html")
        html = html.replace(old, new)
    for out in (os.path.join(HERE, "index.html"), os.path.join(ROOT, "index.html")):
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
    print(f"index.html: {len(html.encode()) / 1024:.0f} KB — {len(dati)} orari, dal {feed['dal']} al {feed['al']}")


if __name__ == "__main__":
    main()
