#!/usr/bin/env python3
"""
Refresh the data block inside index.html for the WA Diesel Watch page.

Updates only what can be parsed reliably:
  * pump prices per centre, from the FuelWatch WA XML feed
  * the Perth terminal gate price, from the AIP terminal gate page

Everything else on the page (national averages, Singapore gasoil, stock and
days of cover, the state reserve, member reports) is maintained by hand and is
never touched here.

Rules, in order of importance:
  1. never publish a figure we are not sure of - carry the old one forward
  2. reject anything outside sanity bounds as a parse error, not a market move
  3. never show a fresh number beside a stale date
  4. fail loudly (exit 1) when a source has failed twice running

Usage:
  python3 scripts/update_page.py                 # fetch live, rewrite index.html
  python3 scripts/update_page.py --dry-run       # parse and report, write nothing
  python3 scripts/update_page.py --offline DIR   # read fixtures from DIR instead of HTTP
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PAGE = os.path.join(ROOT, "index.html")
STATE = os.path.join(ROOT, "state.json")
DEBUG_DIR = os.path.join(ROOT, "debug")

UA = "TruLanded-DieselWatch/1.0 (+https://github.com/nettoholly-create/diesel-watch)"
TIMEOUT = 45

PRICE_MIN, PRICE_MAX = 100.0, 500.0   # c/L; anything outside is a parse error
MAX_DAILY_MOVE = 25.0                 # c/L; a bigger jump is a parse error

FUELWATCH = "https://www.fuelwatch.wa.gov.au/fuelwatch/fuelWatchRSS"
AIP_TGP = "https://aip.com.au/pricing/terminal-gate-prices/"

# Centre name on the page -> FuelWatch query. Names are fixed here on purpose:
# the feed returns suburbs, not centres, so they cannot be derived from the data.
CENTRES = [
    ("Perth (inner)",       {"Suburb": "PERTH",        "Surrounding": "yes"}),
    ("Rockingham-Kwinana",  {"Suburb": "ROCKINGHAM",   "Surrounding": "yes"}),
    ("Geraldton",           {"Suburb": "GERALDTON",    "Surrounding": "yes"}),
    ("Northam",             {"Suburb": "NORTHAM",      "Surrounding": "yes"}),
    ("Merredin",            {"Suburb": "MERREDIN",     "Surrounding": "yes"}),
    ("Narrogin",            {"Suburb": "NARROGIN",     "Surrounding": "yes"}),
    ("Katanning",           {"Suburb": "KATANNING",    "Surrounding": "yes"}),
    ("Albany",              {"Suburb": "ALBANY",       "Surrounding": "yes"}),
    ("Bunbury",             {"Suburb": "BUNBURY",      "Surrounding": "yes"}),
    ("Esperance",           {"Suburb": "ESPERANCE",    "Surrounding": "yes"}),
    ("Port Hedland",        {"Suburb": "PORT HEDLAND", "Surrounding": "yes"}),
    ("Kununurra",           {"Suburb": "KUNUNURRA",    "Surrounding": "yes"}),
    ("Kalgoorlie-Boulder",  {"Region": "1"}),
]

# Centres that appear in the dumbbell chart, in the page's own naming.
CHART_CENTRES = {
    "Esperance", "Albany", "Bunbury", "Rockingham–Kwinana", "Katanning",
    "Geraldton", "Perth (inner)", "Merredin", "Kalgoorlie–Boulder", "Northam",
}

# WA region -> the centres feeding it. Not derivable from the feed.
REGION_CENTRES = {
    "Kimberley": ["Kununurra"],
    "Goldfields–Esperance": ["Kalgoorlie-Boulder", "Esperance"],
    "Great Southern": ["Albany", "Katanning"],
    "South West": ["Bunbury"],
    "Perth metro": ["Perth (inner)", "Rockingham-Kwinana"],
    "Mid West": ["Geraldton"],
    "Wheatbelt": ["Northam", "Merredin", "Narrogin"],
    "Pilbara": ["Port Hedland"],
}

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


# ----------------------------------------------------------------- plumbing

def log(msg):
    print(msg, flush=True)


def fetch(url, params=None, offline=None, fixture=None):
    """Return page text. In offline mode read a fixture file instead."""
    if offline:
        path = os.path.join(offline, fixture)
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    if params:
        url = url + "?" + "&".join(
            "%s=%s" % (k, urllib.parse.quote(str(v))) for k, v in params.items())
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def keep_debug(name, text):
    """Stash a fetched source so a failed run can be diagnosed from the logs."""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        with open(os.path.join(DEBUG_DIR, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass


def sane(value, previous=None):
    """True when a parsed price is plausible."""
    if value is None:
        return False
    if not (PRICE_MIN <= value <= PRICE_MAX):
        return False
    if previous is not None and abs(value - previous) > MAX_DAILY_MOVE:
        return False
    return True


def fmt_label(iso):
    y, m, d = (int(x) for x in iso.split("-"))
    return "%d %s" % (d, MONTHS[m - 1][:3])


# ------------------------------------------------------------- fuelwatch

def local(tag):
    return tag.split("}")[-1].lower()


def parse_fuelwatch(xml_text):
    """Return [{price, site, suburb}] for every station in the feed."""
    out = []
    root = ET.fromstring(xml_text)
    for item in root.iter():
        if local(item.tag) != "item":
            continue
        fields = {}
        for child in item:
            fields[local(child.tag)] = (child.text or "").strip()
        raw = fields.get("price") or ""
        m = re.search(r"\d+(?:\.\d+)?", raw)
        if not m:
            continue
        price = float(m.group(0))
        site = (fields.get("trading-name") or fields.get("brand")
                or fields.get("title") or "").strip()
        suburb = (fields.get("location") or "").strip()
        out.append({"price": price, "site": site, "suburb": suburb})
    return out


def collect_pump_prices(offline):
    """Fetch every centre. Returns (per_centre, cheapest, failures)."""
    per_centre, failures = {}, []
    cheapest = None
    for name, params in CENTRES:
        fixture = "fuelwatch-%s.xml" % re.sub(r"[^a-z0-9]+", "-", name.lower())
        try:
            text = fetch(FUELWATCH, params, offline, fixture)
            stations = parse_fuelwatch(text)
        except (urllib.error.URLError, ET.ParseError, OSError, ValueError) as exc:
            log("  ! %s: %s" % (name, exc))
            failures.append(name)
            continue
        prices = [s for s in stations if sane(s["price"])]
        if not prices:
            log("  ! %s: no usable prices in feed" % name)
            failures.append(name)
            continue
        lo = min(prices, key=lambda s: s["price"])
        hi = max(prices, key=lambda s: s["price"])
        per_centre[name] = {
            "low": round(lo["price"], 1),
            "high": round(hi["price"], 1),
            "n": len(prices),
            "low_site": lo["site"],
        }
        if cheapest is None or lo["price"] < cheapest["price"]:
            cheapest = {"price": lo["price"], "site": lo["site"], "centre": name}
        log("  %-20s %5.1f - %5.1f  (%d sites)"
            % (name, per_centre[name]["low"], per_centre[name]["high"], len(prices)))
    return per_centre, cheapest, failures


# ------------------------------------------------------------------- aip

def strip_tags(html):
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"[ \t]+", " ", html)


def numbers_in_range(text):
    vals = []
    for m in re.finditer(r"\b(\d{3}(?:\.\d)?)\b", text):
        v = float(m.group(1))
        if PRICE_MIN <= v <= PRICE_MAX:
            vals.append(v)
    return vals


def parse_dates(text):
    """
    Every full date in the page: 'Friday, 18th September 2026', '18 Sep 2026'.
    The year is required - a bare 'Fri 25 Sep' is too ambiguous to trust.
    """
    found = []
    names = "|".join(MONTHS) + "|" + "|".join(m[:3] for m in MONTHS)
    pattern = re.compile(
        r"(\d{1,2})(?:st|nd|rd|th)?\s+(" + names + r")\.?\s+(\d{4})", re.I)
    for m in pattern.finditer(text):
        day, month, year = int(m.group(1)), m.group(2).title(), int(m.group(3))
        idx = next((i for i, name in enumerate(MONTHS)
                    if name == month or name[:3] == month[:3]), None)
        if idx is None:
            continue
        try:
            found.append(dt.date(year, idx + 1, day))
        except ValueError:
            continue
    return found


def parse_perth_tgp(html):
    """
    Return (series, date) where series is that week's Perth diesel TGP values in
    page order and date is the day the last value belongs to. Either may be None.
    """
    series = None

    # First try: the table row that names Perth.
    for row in re.findall(r"(?is)<tr\b.*?</tr>", html):
        if not re.search(r"(?i)\bperth\b", strip_tags(row)):
            continue
        vals = numbers_in_range(strip_tags(row))
        if vals:
            series = vals
            break

    # Fallback: the run of numbers following the word Perth in the flat text.
    if not series:
        text = strip_tags(html)
        m = re.search(r"(?i)\bperth\b", text)
        if m:
            vals = numbers_in_range(text[m.end():m.end() + 260])
            if vals:
                series = vals[:5]

    # The page carries the last five published weekdays. Take the most recent
    # date that has actually happened; a future date means we misread the page.
    today = dt.date.today()
    dates = [d for d in parse_dates(strip_tags(html)) if d <= today]
    latest = max(dates) if dates else None
    return series, latest


def collect_tgp(offline, previous):
    """Returns (value, series, date) - any of them None on a parse failure."""
    try:
        html = fetch(AIP_TGP, None, offline, "aip-tgp.html")
    except (urllib.error.URLError, OSError) as exc:
        log("  ! terminal gate price: %s" % exc)
        return None, None, None
    keep_debug("aip-tgp.html", html)
    series, date = parse_perth_tgp(html)
    if not series:
        log("  ! terminal gate price: could not find Perth in the page")
        return None, None, None
    value = series[-1]
    if not sane(value, previous):
        log("  ! terminal gate price: %.1f rejected by sanity bounds (previous %s)"
            % (value, previous))
        return None, None, None
    if date is None:
        log("  ! terminal gate price: found %.1f but no date - not publishing it"
            % value)
        return None, None, None
    log("  Perth terminal gate  %5.1f  (%s, week %s)"
        % (value, date.isoformat(), ", ".join("%.1f" % v for v in series)))
    return value, series, date


# ------------------------------------------------------------------ page

BLOCK = re.compile(
    r'(<script id="dw-data" type="application/json">\n)(.*?)(\n</script>)', re.S)


def read_page():
    with open(PAGE, encoding="utf-8") as fh:
        html = fh.read()
    m = BLOCK.search(html)
    if not m:
        raise SystemExit("index.html has no dw-data block - wrong file or hand-edited")
    return html, json.loads(m.group(2))


def write_page(html, data):
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    new = BLOCK.sub(lambda m: m.group(1) + payload + m.group(3), html, count=1)
    with open(PAGE, "w", encoding="utf-8") as fh:
        fh.write(new)


def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    return {"fails": {}, "last_good": {}}


def save_state(state):
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


# ------------------------------------------------------------------ main

def apply_pump_prices(data, per_centre, cheapest):
    """Rewrite towns, the price/site counts in regions, and the cheapest tile."""
    changed = False

    # The chart uses en-dashed names; the fetch list uses plain hyphens.
    def page_name(name):
        return name.replace("-", "–") if name.replace("-", "–") in CHART_CENTRES else name

    towns = []
    for name, vals in per_centre.items():
        pname = page_name(name)
        if pname in CHART_CENTRES:
            towns.append({"name": pname, "low": vals["low"],
                          "high": vals["high"], "n": vals["n"]})
    if len(towns) == len(CHART_CENTRES):
        towns.sort(key=lambda t: t["low"])
        if towns != data["towns"]:
            data["towns"] = towns
            changed = True
    else:
        missing = CHART_CENTRES - {t["name"] for t in towns}
        log("  ! chart centres missing, keeping previous towns: %s"
            % ", ".join(sorted(missing)))

    for region in data["regions"]:
        members = [per_centre[c] for c in REGION_CENTRES.get(region["name"], [])
                   if c in per_centre]
        if not members:
            log("  ! no data for region %s, keeping previous" % region["name"])
            continue
        low = round(min(m["low"] for m in members), 1)
        high = round(max(m["high"] for m in members), 1)
        sites = sum(m["n"] for m in members)
        if (region["low"], region["high"], region["sites"]) != (low, high, sites):
            region["low"], region["high"], region["sites"] = low, high, sites
            changed = True

    if cheapest:
        tile = data["tiles"]["price"][1]
        val = "%.1f" % cheapest["price"]
        note = cheapest["site"] or cheapest["centre"]
        if (tile.get("val"), tile.get("note")) != (val, note):
            tile["val"], tile["note"] = val, note
            tile.pop("delta", None)
            changed = True
    return changed


def apply_tgp(data, value, series, date):
    changed = False
    tile = data["tiles"]["price"][0]
    val = "%.1f" % value
    if tile.get("val") != val:
        tile["val"] = val
        changed = True

    if series and len(series) >= 2:
        move = value - series[0]
        delta = {"dir": "down" if move < 0 else "up",
                 "amount": "%.1f c/L" % abs(move),
                 "text": "over the week"}
        if tile.get("delta") != delta:
            tile["delta"] = delta
            tile.pop("note", None)
            changed = True

    iso = date.isoformat()
    bench = "%s %d %s %d" % (
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
         "Saturday", "Sunday"][date.weekday()],
        date.day, MONTHS[date.month - 1], date.year)
    if data.get("benchDate") != bench:
        data["benchDate"] = bench
        changed = True

    # The final weekly row is the latest daily terminal gate point.
    last = data["weekly"][-1]
    if last.get("pump") is None and last.get("gas") is None:
        if (last["d"], last["tgp"]) != (iso, value):
            last["d"], last["tgp"], last["label"] = iso, value, fmt_label(iso)
            changed = True
    else:
        data["weekly"].append({"d": iso, "label": fmt_label(iso),
                               "tgp": value, "pump": None, "gas": None})
        changed = True
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--offline", metavar="DIR")
    args = ap.parse_args()

    html, data = read_page()
    state = load_state()
    fails = state.setdefault("fails", {})
    last_good = state.setdefault("last_good", {})

    log("Pump prices")
    per_centre, cheapest, centre_failures = collect_pump_prices(args.offline)

    log("Terminal gate price")
    previous = last_good.get("tgp", {}).get("value")
    tgp, series, tgp_date = collect_tgp(args.offline, previous)

    changed = False
    today = dt.date.today().isoformat()

    if per_centre:
        changed |= apply_pump_prices(data, per_centre, cheapest)
        if data.get("asof") != today:
            data["asof"] = today
            changed = True
        fails["fuelwatch"] = len(centre_failures)
        last_good["fuelwatch"] = {"date": today, "centres": len(per_centre)}
    else:
        fails["fuelwatch"] = fails.get("fuelwatch", 0) + 1
        log("  ! no pump prices at all, page left as it was")

    if tgp is not None:
        changed |= apply_tgp(data, tgp, series, tgp_date)
        fails["tgp"] = 0
        last_good["tgp"] = {"value": tgp, "date": tgp_date.isoformat(),
                            "seen": today}
    else:
        fails["tgp"] = fails.get("tgp", 0) + 1
        log("  ! terminal gate price carried forward (%s consecutive failures)"
            % fails["tgp"])

    if args.dry_run:
        log("\ndry run: %s" % ("changes found" if changed else "nothing to change"))
        return 0

    if changed:
        write_page(html, data)
        log("\nindex.html updated")
    else:
        log("\nno change")

    state["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    save_state(state)

    # One line the workflow turns into the commit message.
    bits = []
    if cheapest:
        bits.append("cheapest %.1f %s" % (cheapest["price"], cheapest["site"]))
    if tgp is not None:
        bits.append("Perth TGP %.1f (%s)" % (tgp, fmt_label(tgp_date.isoformat())))
    log("SUMMARY: pump prices %s%s"
        % (fmt_label(today), (", " + ", ".join(bits)) if bits else ""))

    broken = [name for name, count in fails.items() if count >= 2]
    if broken:
        log("\nFAILING: %s has failed twice or more in a row" % ", ".join(broken))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
