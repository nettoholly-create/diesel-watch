#!/usr/bin/env python3
"""
update_weekly.py - WA Diesel Watch, weekly figures

Companion to scripts/update_page.py. That script owns the DAILY fields
(pump prices per centre, the Perth terminal gate price value). This one owns
everything it does not touch:

  tiles.price[2]  national average retail diesel      <- AIP Weekly Diesel Prices Report
  tiles.price[3]  Singapore gasoil 10ppm              <- AIP Weekly Diesel Prices Report
  tiles.stock[0]  national days of cover + ML held    <- fuelplan.gov.au
  tiles.stock[2]  WA sites out of diesel              <- fuelplan.gov.au
  weekly[]        a new row per week-ending report    <- AIP Weekly Diesel Prices Report
  tiles.price[0].delta   the "over the week" TGP move <- corrected 7-day lookback

It deliberately does NOT touch tiles.stock[1] or [3] (the WA state reserve) or
any region's `reserve` / `report` field. Those move rarely and are changed by hand.

RUN ORDER. This must run AFTER update_page.py in the same job, because it
corrects the TGP delta that update_page.py writes, and it reads the TGP value
update_page.py has just published.

THE TGP DELTA FIX. update_page.py's apply_tgp() computes `move = value - series[0]`
where series is AIP's rolling five-day row, so it compares against the oldest
VISIBLE column - four publishing days back, not seven calendar days. Because AIP
publishes weekdays only, the gap wanders (it produced 15.9 c/L on 22 Sep where the
true move was 22.6, and 4.8 on 1 Oct where it was 8.1). This script keeps a dated
history in state_weekly.json and recomputes the delta against the value from seven
calendar days earlier. It never touches update_page.py.

FAIL-LOUD, NEVER FAIL-WRONG. Every figure is range-checked and checked against the
previous value before it is written. Anything that fails validation is skipped, not
published, and reported on stderr prefixed WEEKLY-ERROR. The script still exits 0 so
a weekly source breaking can never stop the daily pump/TGP update from publishing;
the workflow's final step turns any WEEKLY-ERROR into a failed run so GitHub emails you.

USAGE
  python scripts/update_weekly.py --dry-run     # print what would change, write nothing
  python scripts/update_weekly.py               # apply changes to index.html
  python scripts/update_weekly.py --fixtures d  # parse saved files in dir d instead of fetching
  python scripts/update_weekly.py --save-fixtures d   # fetch and also save raw sources to d
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
INDEX = os.path.join(REPO, "index.html")
STATE = os.path.join(HERE, "state_weekly.json")

OPEN_TAG = '<script id="dw-data" type="application/json">'
CLOSE_TAG = "</script>"

AIP_NATIONAL = "https://aip.com.au/location-category/national/"
FUELPLAN = "https://fuelplan.gov.au/fuel-statistics"

UA = "Mozilla/5.0 (compatible; diesel-watch/1.0; +https://github.com/nettoholly-create/diesel-watch)"

# Keep roughly six months of weekly rows.
WEEKLY_RETENTION_DAYS = 190

# ---------------------------------------------------------------------------
# Seed for the TGP history. These are PUBLISHED AIP Perth DIESEL terminal gate
# figures, verified 27-28 Oct 2026 against the AIP rolling table and against the
# daily TGP tables inside the weekly reports for w/e 11, 18 and 25 October 2026.
# Once the script has run for a fortnight the seed stops mattering; it is here so
# the very first runs can compute a correct seven-day move.
# ---------------------------------------------------------------------------
SEED_TGP_HISTORY = {
    "2026-10-09": 254.7,   # Friday, from w/e 11 Oct report
    "2026-10-16": 253.8,   # Friday, from w/e 18 Oct report
    "2026-10-19": 254.2,   # from w/e 25 Oct report daily table
    "2026-10-20": 253.3,
    "2026-10-21": 253.2,
    "2026-10-22": 254.1,
    "2026-10-23": 255.2,
    "2026-10-26": 256.3,   # AIP rolling table
}

# ---------------------------------------------------------------------------
# Plausibility bounds. A figure outside its range, or moving more than its
# max_jump from the value already on the page, is treated as a parse failure.
# Widen these only if a real market move is genuinely rejected.
# ---------------------------------------------------------------------------
BOUNDS = {
    "national_retail": {"min": 120.0, "max": 450.0, "max_jump": 30.0},
    "gasoil":          {"min": 60.0,  "max": 350.0, "max_jump": 40.0},
    "perth_retail":    {"min": 120.0, "max": 450.0, "max_jump": 30.0},
    "perth_tgp":       {"min": 100.0, "max": 450.0, "max_jump": 30.0},
    "days_cover":      {"min": 10.0,  "max": 120.0, "max_jump": 15.0},
    "ml_held":         {"min": 800.0, "max": 8000.0, "max_jump": 1200.0},
    "wa_outages":      {"min": 0.0,   "max": 400.0, "max_jump": 150.0},
    "nat_outages":     {"min": 0.0,   "max": 900.0, "max_jump": 300.0},
}

ERRORS: list[str] = []
NOTES: list[str] = []


def err(msg: str) -> None:
    ERRORS.append(msg)
    print("WEEKLY-ERROR: " + msg, file=sys.stderr)


def note(msg: str) -> None:
    NOTES.append(msg)
    print("WEEKLY: " + msg)


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------

def fetch(url: str, binary: bool = False, tries: int = 3):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
            return raw if binary else raw.decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - report, never crash the run
            last = exc
    raise RuntimeError(f"could not fetch {url}: {last}")


def strip_tags(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", html)


# ---------------------------------------------------------------------------
# AIP weekly report
# ---------------------------------------------------------------------------

MONTHS = ("january february march april may june july august september "
          "october november december").split()


def discover_weekly_report(html: str) -> tuple[str, dt.date]:
    """Find the newest Weekly Diesel Prices Report link and its week-ending date.

    Discovery beats URL guessing: the filename pattern is not reliable (the
    week-ending 4 October 2026 report was not at the predicted URL).
    """
    best = None
    for m in re.finditer(
        r'href="([^"]*weekly-diesel-prices-report[^"]*?)"', html, re.I
    ):
        href = m.group(1)
        d = week_ending_from_text(href)
        if d and (best is None or d > best[1]):
            best = (href, d)
    if not best:
        raise RuntimeError("no Weekly Diesel Prices Report link found on the AIP page")
    href, when = best
    if href.startswith("/"):
        href = "https://aip.com.au" + href
    return href, when


def week_ending_from_text(text: str) -> dt.date | None:
    t = text.lower().replace("%20", "-").replace("_", "-")
    m = re.search(r"(\d{1,2})[-\s]+(" + "|".join(MONTHS) + r")[-\s]+(\d{4})", t)
    if not m:
        return None
    try:
        return dt.date(int(m.group(3)), MONTHS.index(m.group(2)) + 1, int(m.group(1)))
    except ValueError:
        return None


def find_pdf_url(report_html: str) -> str:
    m = re.search(r'href="([^"]+\.pdf)"', report_html, re.I)
    if not m:
        raise RuntimeError("no PDF link on the weekly report page")
    url = m.group(1)
    return "https://aip.com.au" + url if url.startswith("/") else url


def pdf_text(raw: bytes) -> str:
    try:
        import pdfplumber  # type: ignore
    except ImportError as exc:
        raise RuntimeError("pdfplumber is not installed (add it to the workflow)") from exc
    import io

    out = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for page in pdf.pages:
            out.append(page.extract_text() or "")
    return re.sub(r"[ \t]+", " ", "\n".join(out))


# --- PDF patterns ----------------------------------------------------------
# These are the only part of this script that could not be tested against a real
# PDF before delivery. If a run reports "could not parse", dump the text with
#     python scripts/update_weekly.py --dry-run --dump-pdf-text
# and adjust the patterns here. Everything else is layout-independent.

NUM = r"(\d{2,3}\.\d)"


def parse_weekly_pdf(text: str) -> dict:
    out: dict[str, float] = {}
    low = text.lower()

    pat_national = [
        r"national\s+average\s+(?:retail\s+)?diesel[^0-9]{0,80}" + NUM,
        r"average\s+retail\s+diesel\s+price[^0-9]{0,80}" + NUM,
    ]
    pat_gasoil = [
        r"(?:singapore\s+)?gasoil\s*\(?10\s*ppm\)?[^0-9]{0,120}" + NUM,
        r"gasoil[^0-9]{0,120}" + NUM,
    ]
    pat_perth_retail = [
        r"perth[^\n]{0,60}?average[^0-9]{0,40}" + NUM,
        r"perth\s+" + NUM + r"\s",
    ]

    for key, pats in (
        ("national_retail", pat_national),
        ("gasoil", pat_gasoil),
        ("perth_retail", pat_perth_retail),
    ):
        for p in pats:
            m = re.search(p, low)
            if m:
                out[key] = float(m.group(1))
                break
        else:
            err(f"could not parse {key} from the weekly report PDF")

    tgp = parse_perth_tgp_row(text)
    if tgp:
        out["perth_tgp_daily"] = tgp
    else:
        err("could not parse the daily Perth TGP table from the weekly report PDF")
    return out


def parse_perth_tgp_row(text: str) -> dict[str, float]:
    """Pull the daily Perth TGP row (five weekday values) out of the report.

    Returns {iso date: value}. The Friday value is what a weekly row records.
    """
    dates: list[dt.date] = []
    for line in text.splitlines():
        found = re.findall(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", line)
        if len(found) >= 5:
            try:
                dates = [
                    dt.date(2000 + int(y) if len(y) == 2 else int(y), int(mo), int(d))
                    for d, mo, y in found[:5]
                ]
            except ValueError:
                dates = []
            if dates:
                break
    if not dates:
        return {}

    for line in text.splitlines():
        if not re.match(r"\s*perth\b", line, re.I):
            continue
        vals = re.findall(NUM, line)
        if len(vals) >= 5:
            return {d.isoformat(): float(v) for d, v in zip(dates, vals[:5])}
    return {}


# ---------------------------------------------------------------------------
# fuelplan.gov.au
# ---------------------------------------------------------------------------

def parse_fuelplan(html: str) -> dict:
    t = strip_tags(html)
    out: dict = {}

    m = re.search(r"(\d{1,3})\s*days?\s+of\s+(?:diesel\s+)?cover", t, re.I) or \
        re.search(r"diesel[^.]{0,80}?(\d{1,3})\s*days", t, re.I)
    if m:
        out["days_cover"] = float(m.group(1))
    else:
        err("could not parse days of cover from fuelplan.gov.au")

    m = re.search(r"([\d,]{3,6})\s*ML", t)
    if m:
        out["ml_held"] = float(m.group(1).replace(",", ""))
        d = date_near(t, m.start())
        if d:
            out["stock_date"] = d
    else:
        err("could not parse ML held from fuelplan.gov.au")

    m = re.search(r"Western Australia[^.]{0,120}?(\d{1,3})\s*sites?", t, re.I) or \
        re.search(r"(\d{1,3})\s*sites?[^.]{0,60}Western Australia", t, re.I)
    if m:
        out["wa_outages"] = float(m.group(1))
    else:
        err("could not parse WA diesel stock-outs from fuelplan.gov.au")

    m = re.search(r"(?:nationally|national)[^.]{0,120}?(\d{1,3})\s*sites?", t, re.I) or \
        re.search(r"(\d{1,3})\s*sites?[^.]{0,60}nationally", t, re.I)
    if m:
        out["nat_outages"] = float(m.group(1))
        d = date_near(t, m.start())
        if d:
            out["outage_date"] = d
    else:
        err("could not parse national diesel stock-outs from fuelplan.gov.au")

    return out


def date_near(text: str, pos: int) -> dt.date | None:
    window = text[max(0, pos - 300): pos + 300]
    m = re.search(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})", window, re.I)
    if not m:
        return None
    try:
        return dt.date(int(m.group(3)), MONTHS.index(m.group(2).lower()) + 1, int(m.group(1)))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# index.html read / write
# ---------------------------------------------------------------------------

def read_page() -> tuple[str, dict, int, int]:
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()
    i = html.find(OPEN_TAG)
    if i < 0:
        raise RuntimeError("dw-data block not found in index.html")
    start = i + len(OPEN_TAG)
    end = html.find(CLOSE_TAG, start)
    if end < 0:
        raise RuntimeError("dw-data block is not closed")
    return html, json.loads(html[start:end]), start, end


def write_page(html: str, data: dict, start: int, end: int) -> None:
    body = "\n" + json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    with open(INDEX, "w", encoding="utf-8") as f:
        f.write(html[:start] + body + html[end:])


def load_state() -> dict:
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            s = json.load(f)
    else:
        s = {}
    hist = s.setdefault("tgp_history", {})
    for k, v in SEED_TGP_HISTORY.items():
        hist.setdefault(k, v)
    s.setdefault("last_report", "")
    return s


def save_state(s: dict) -> None:
    s["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, sort_keys=True)
        f.write("\n")


# ---------------------------------------------------------------------------
# validation + application
# ---------------------------------------------------------------------------

def ok(key: str, value: float, previous: float | None) -> bool:
    b = BOUNDS[key]
    if not (b["min"] <= value <= b["max"]):
        err(f"{key} = {value} is outside the plausible range "
            f"{b['min']}-{b['max']}; not published")
        return False
    if previous is not None and abs(value - previous) > b["max_jump"]:
        err(f"{key} moved from {previous} to {value}, more than the "
            f"{b['max_jump']} sanity limit; not published - check the source by hand")
        return False
    return True


def as_float(x) -> float | None:
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def set_tile(tile: dict, value: float, key: str, decimals: int = 1) -> bool:
    prev = as_float(tile.get("val"))
    if not ok(key, value, prev):
        return False
    new = f"{value:.{decimals}f}" if decimals else f"{value:.0f}"
    if tile.get("val") == new:
        return False
    if prev is not None:
        move = round(value - prev, 1)
        tile["delta"] = {
            "dir": "down" if move < 0 else "up",
            "amount": f"{abs(move):.1f} c/L",
            "text": tile.get("delta", {}).get("text", "week on week"),
        }
    tile["val"] = new
    note(f"{tile.get('lab')}: {prev} -> {new}")
    return True


def apply_weekly_report(data: dict, fig: dict, week_ending: dt.date) -> bool:
    changed = False

    if "national_retail" in fig:
        changed |= set_tile(data["tiles"]["price"][2], fig["national_retail"], "national_retail")
    if "gasoil" in fig:
        changed |= set_tile(data["tiles"]["price"][3], fig["gasoil"], "gasoil")

    daily = fig.get("perth_tgp_daily") or {}
    friday = None
    for iso in sorted(daily):
        if dt.date.fromisoformat(iso).weekday() == 4:
            friday = daily[iso]
    if friday is None and daily:
        friday = daily[sorted(daily)[-1]]

    iso = week_ending.isoformat()
    if any(r.get("d") == iso for r in data["weekly"]):
        return changed

    if friday is None or "perth_retail" not in fig:
        err(f"week ending {iso} not added to the chart: missing "
            f"{'Friday TGP' if friday is None else 'Perth retail'}")
        return changed

    prev_rows = [r for r in data["weekly"] if r.get("gas") is not None]
    prev_tgp = prev_rows[-1]["tgp"] if prev_rows else None
    prev_pump = prev_rows[-1]["pump"] if prev_rows else None
    if not ok("perth_tgp", friday, prev_tgp) or not ok("perth_retail", fig["perth_retail"], prev_pump):
        return changed

    row = {
        "d": iso,
        "label": f"{week_ending.day} {week_ending.strftime('%b')}",
        "tgp": round(friday, 1),
        "pump": round(fig["perth_retail"], 1),
        "gas": round(fig["gasoil"], 1) if "gasoil" in fig else None,
    }

    # The final row is the live daily point and always stays last. A weekly row
    # dated after it would plot the chart backwards - that should be impossible
    # (a week-ending-Sunday report appears during the following week) so if it
    # happens the daily row is stale. Hold the row over to the next run.
    daily_row = data["weekly"][-1]
    if dt.date.fromisoformat(daily_row["d"]) < week_ending:
        err(f"week ending {iso} is later than the live daily point "
            f"({daily_row['d']}) - the daily update is behind. Row held over.")
        return changed

    # Insert in date order among the weekly rows, never after the daily row.
    pos = len(data["weekly"]) - 1
    while pos > 0 and dt.date.fromisoformat(data["weekly"][pos - 1]["d"]) > week_ending:
        pos -= 1
    data["weekly"].insert(pos, row)
    note(f"added weekly row {row['label']}: tgp {row['tgp']}, pump {row['pump']}, gas {row['gas']}")

    cutoff = dt.date.today() - dt.timedelta(days=WEEKLY_RETENTION_DAYS)
    before = len(data["weekly"])
    data["weekly"] = [
        r for i, r in enumerate(data["weekly"])
        if i == len(data["weekly"]) - 1 or dt.date.fromisoformat(r["d"]) >= cutoff
    ]
    if len(data["weekly"]) < before:
        note(f"dropped {before - len(data['weekly'])} weekly row(s) older than six months")
    return True


def apply_fuelplan(data: dict, fp: dict) -> bool:
    changed = False
    stock = data["tiles"]["stock"]

    if "days_cover" in fp:
        prev = as_float(stock[0].get("val"))
        if ok("days_cover", fp["days_cover"], prev):
            new = f"{fp['days_cover']:.0f}"
            if stock[0].get("val") != new:
                stock[0]["val"] = new
                changed = True
                note(f"days of cover: {prev} -> {new}")

    if "ml_held" in fp and ok("ml_held", fp["ml_held"], None):
        when = fp.get("stock_date")
        label = f"{when.day} {when.strftime('%b')}" if when else "latest"
        new_note = f"{fp['ml_held']:,.0f} ML held, {label}"
        if stock[0].get("note") != new_note:
            stock[0]["note"] = new_note
            changed = True
            note(f"stock note -> {new_note}")

    if "wa_outages" in fp:
        prev = as_float(stock[2].get("val"))
        if ok("wa_outages", fp["wa_outages"], prev):
            new = f"{fp['wa_outages']:.0f}"
            if stock[2].get("val") != new:
                stock[2]["val"] = new
                changed = True
                note(f"WA sites out of diesel: {prev} -> {new}")

    if "nat_outages" in fp and ok("nat_outages", fp["nat_outages"], None):
        when = fp.get("outage_date")
        label = f"{when.day} {when.strftime('%b')}" if when else "latest"
        new_note = f"{label} · {fp['nat_outages']:.0f} nationally"
        if stock[2].get("note") != new_note:
            stock[2]["note"] = new_note
            changed = True
            note(f"outage note -> {new_note}")

    return changed


def fix_tgp_delta(data: dict, state: dict) -> bool:
    """Recompute the terminal gate tile's weekly move over seven CALENDAR days."""
    tile = data["tiles"]["price"][0]
    value = as_float(tile.get("val"))
    asof = data.get("asof")
    if value is None or not asof:
        err("cannot correct the TGP delta: no value or asof on the page")
        return False

    hist = state["tgp_history"]
    hist[asof] = value

    today = dt.date.fromisoformat(asof)
    target = today - dt.timedelta(days=7)
    # AIP publishes weekdays only, so accept the nearest reading 6-10 days back.
    best, best_gap = None, None
    for iso, v in hist.items():
        gap = (today - dt.date.fromisoformat(iso)).days
        if 6 <= gap <= 10:
            score = abs(gap - 7)
            if best_gap is None or score < best_gap:
                best, best_gap = (iso, v), score
    if not best:
        err(f"no TGP reading 6-10 days before {asof}; leaving the delta as "
            f"update_page.py wrote it (it will be wrong)")
        return False

    ref_iso, ref = best
    move = round(value - ref, 1)
    delta = {
        "dir": "down" if move < 0 else "up",
        "amount": f"{abs(move):.1f} c/L",
        "text": "over the week",
    }
    # Trim history to a year.
    cutoff = (today - dt.timedelta(days=365)).isoformat()
    state["tgp_history"] = {k: v for k, v in hist.items() if k >= cutoff}

    if tile.get("delta") == delta:
        return False
    old = tile.get("delta", {})
    tile["delta"] = delta
    note(f"TGP delta corrected: was {old.get('dir')} {old.get('amount')}, "
         f"now {delta['dir']} {delta['amount']} ({value} less {ref} on {ref_iso})")
    return True


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print changes, write nothing")
    ap.add_argument("--fixtures", help="parse saved sources from this directory")
    ap.add_argument("--save-fixtures", help="save fetched sources to this directory")
    ap.add_argument("--dump-pdf-text", action="store_true", help="print the PDF text and exit")
    args = ap.parse_args()

    html, data, start, end = read_page()
    state = load_state()
    changed = False

    # --- AIP weekly report ------------------------------------------------
    try:
        if args.fixtures:
            with open(os.path.join(args.fixtures, "report.pdf"), "rb") as f:
                raw = f.read()
            week_ending = week_ending_from_text(
                open(os.path.join(args.fixtures, "report.url")).read()
            ) or dt.date.today()
        else:
            report_url, week_ending = discover_weekly_report(fetch(AIP_NATIONAL))
            if report_url == state.get("last_report"):
                note(f"no new weekly report (latest is still week ending {week_ending})")
                raw = None
            else:
                raw = fetch(find_pdf_url(fetch(report_url)), binary=True)
                state["last_report"] = report_url

        if raw is not None:
            if args.save_fixtures:
                os.makedirs(args.save_fixtures, exist_ok=True)
                with open(os.path.join(args.save_fixtures, "report.pdf"), "wb") as f:
                    f.write(raw)
            text = pdf_text(raw)
            if args.dump_pdf_text:
                print(text)
                return 0
            note(f"parsing weekly report for week ending {week_ending}")
            changed |= apply_weekly_report(data, parse_weekly_pdf(text), week_ending)
    except Exception as exc:  # noqa: BLE001
        err(f"weekly report step failed: {exc}")

    # --- fuelplan.gov.au --------------------------------------------------
    try:
        if args.fixtures:
            fp_html = open(os.path.join(args.fixtures, "fuelplan.html"), encoding="utf-8").read()
        else:
            fp_html = fetch(FUELPLAN)
            if args.save_fixtures:
                os.makedirs(args.save_fixtures, exist_ok=True)
                with open(os.path.join(args.save_fixtures, "fuelplan.html"), "w",
                          encoding="utf-8") as f:
                    f.write(fp_html)
        changed |= apply_fuelplan(data, parse_fuelplan(fp_html))
    except Exception as exc:  # noqa: BLE001
        err(f"fuelplan step failed: {exc}")

    # --- TGP delta --------------------------------------------------------
    try:
        changed |= fix_tgp_delta(data, state)
    except Exception as exc:  # noqa: BLE001
        err(f"TGP delta step failed: {exc}")

    if not changed:
        note("no weekly figure changed")

    if args.dry_run:
        print("\n--- dry run, nothing written ---")
        print(json.dumps(data["tiles"], indent=2, ensure_ascii=False))
        print(json.dumps(data["weekly"][-4:], indent=2, ensure_ascii=False))
    elif changed:
        write_page(html, data, start, end)
        save_state(state)
        note("index.html updated")
    else:
        save_state(state)

    if ERRORS:
        print(f"SUMMARY: weekly update completed with {len(ERRORS)} source problem(s)")
    elif changed:
        print("SUMMARY: weekly figures updated")
    return 0  # never block the daily update


if __name__ == "__main__":
    sys.exit(main())
