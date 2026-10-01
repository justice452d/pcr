"""Refresh and serve the local equity/bond and monthly valuation datasets."""
from __future__ import annotations

import csv
import json
import math
import re
import statistics
import urllib.request
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "sources"
INDEXES = {
    "sse": ("000001", SOURCES / "bond_sse" / "sse_composite_daily.csv"),
    "csi": ("000985", SOURCES / "bond_csi" / "csi_all_share_daily.csv"),
    # The public CSI endpoint exposes a consistent PE-TTM series for SSE 50.
    # It is presented in the UI as the transparent onshore CHINA50 proxy.
    "china50": ("000016", SOURCES / "bond_china50" / "china50_daily.csv"),
}
CSI_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode={}&startDate={}&endDate={}"
BOND_URL = ("https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery"
            "?startDate={}&endDate={}&gjqx=0&qxId=ycqx&locale=cn_ZH")


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def refresh_index(key, full=False):
    code, path = INDEXES[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                rows[row["date"]] = row
    if full or not rows:
        years = range(2011, date.today().year + 1)
    else:
        years = range(max(2011, date.today().year - 1), date.today().year + 1)
    for year in years:
        start, end = f"{year}0101", f"{year}1231"
        payload = fetch_json(CSI_URL.format(code, start, end)).get("data") or []
        for item in payload:
            day = str(item.get("tradeDate") or "")
            close, pe, volume = item.get("close"), item.get("peg"), item.get("tradingVol")
            if len(day) == 8 and close and pe and float(pe) > 0 and (volume is None or float(volume) > 0):
                rows[day] = {"date": day, "close": float(close), "pe_ttm": float(pe),
                             "trading_volume": float(volume or 0)}
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, ["date", "close", "pe_ttm", "trading_volume"])
        writer.writeheader()
        writer.writerows(rows[key] for key in sorted(rows))
    return len(rows)


class Cells(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.in_cell = [], [], [], False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "tr":
            self.row = []
        elif tag.lower() == "td":
            self.cell, self.in_cell = [], True

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "td" and self.in_cell:
            self.row.append("".join(self.cell).strip())
            self.in_cell = False
        elif tag.lower() == "tr" and self.row:
            self.rows.append(self.row)


def refresh_bonds():
    canonical = SOURCES / "bond_csi" / "bond_10y_daily.csv"
    rows = {}
    with canonical.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            rows[row["date"]] = row
    start = date.fromisoformat(max(rows)) - timedelta(days=10)
    end = date.today()
    request = urllib.request.Request(BOND_URL.format(start, end), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8", "ignore")
    parser = Cells()
    parser.feed(text)
    # Results repeat three curve families per day; the first row is the government curve.
    seen_dates = set()
    for row in parser.rows:
        if len(row) >= 9 and re.fullmatch(r"20\d\d-\d\d-\d\d", row[1] or ""):
            if row[1] in seen_dates:
                continue
            try:
                yield10 = float(row[8])
            except ValueError:
                continue
            seen_dates.add(row[1])
            rows[row[1]] = {"date": row[1], "yield10_pct": yield10,
                            "source": "ChinaBond historyQuery"}
    fields = list(next(iter(rows.values())).keys())
    if "source" not in fields:
        fields.append("source")
    for target in (SOURCES / "bond_csi" / "bond_10y_daily.csv",
                   SOURCES / "bond_sse" / "bond_10y_daily.csv",
                   SOURCES / "bond_china50" / "bond_10y_daily.csv"):
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows[key] for key in sorted(rows))
    return len(rows)


def build_market(key):
    _, index_path = INDEXES[key]
    bond_path = SOURCES / ("bond_" + key) / "bond_10y_daily.csv"
    if key == "china50" and not bond_path.exists():
        bond_path = SOURCES / "bond_csi" / "bond_10y_daily.csv"
    bonds = {}
    with bond_path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            bonds[row["date"]] = float(row["yield10_pct"])
    output = []
    with index_path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            raw = row["date"]
            day = raw if "-" in raw else f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
            if day not in bonds:
                continue
            try:
                pe, close, yld = float(row["pe_ttm"]), float(row["close"]), bonds[day]
            except (ValueError, KeyError):
                continue
            if not (5 < pe < 100 and 0 < yld < 10):
                continue
            output.append({"date": day, "index": close, "pe": pe, "yield10": yld,
                           "earnings_yield": 100 / pe, "erp": 100 / pe - yld})
    for index, row in enumerate(output):
        values = [item["erp"] for item in output[max(0, index - 755):index + 1]]
        if len(values) >= 500:
            avg, sd = statistics.fmean(values), statistics.pstdev(values)
            row.update(mean=avg, upper=avg + 2 * sd, lower=avg - 2 * sd,
                       z=(row["erp"] - avg) / sd if sd else 0)
        else:
            row.update(mean=None, upper=None, lower=None, z=None)
    return output


def valuation():
    path = SOURCES / "valuation" / "csi_monthly_study.csv"
    sse_by_month = {}
    with INDEXES["sse"][1].open(newline="", encoding="utf-8-sig") as handle:
        for item in csv.DictReader(handle):
            raw = item.get("date", "")
            if len(raw) >= 6 and item.get("close"):
                sse_by_month[raw[:6]] = float(item["close"])
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            try:
                month = row["date"][:7].replace("-", "")
                rows.append({"date": row["date"], "close": float(row["close"]),
                             "pe": float(row["pe_ttm"]),
                             "percentile": float(row["pe_percentile_10y"]) if row.get("pe_percentile_10y") else None,
                             "sse_close": sse_by_month.get(month)})
            except (ValueError, KeyError):
                continue
    return rows


def rebuild_valuation():
    """Collapse the CSI All Share daily PE series to month-end observations."""
    source = INDEXES["csi"][1]
    by_month = {}
    with source.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row.get("date") and row.get("pe_ttm"):
                by_month[row["date"][:6]] = row
    output = []
    for row in by_month.values():
        raw = row["date"]
        output.append({"date": f"{raw[:4]}-{raw[4:6]}-{raw[6:]}",
                       "close": float(row["close"]), "pe_ttm": float(row["pe_ttm"])})
    for index, row in enumerate(output):
        history = [item["pe_ttm"] for item in output[max(0, index - 119):index + 1]]
        row["history_months"] = len(history)
        row["pe_percentile_10y"] = (round(100 * (sum(value < row["pe_ttm"] for value in history) +
                                                       .5 * sum(value == row["pe_ttm"] for value in history)) /
                                              len(history), 2) if len(history) >= 36 else "")
    target = SOURCES / "valuation" / "csi_monthly_study.csv"
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, ["date", "close", "pe_ttm", "history_months", "pe_percentile_10y"])
        writer.writeheader()
        writer.writerows(output)
    return len(output)


def refresh_all(full_china50=False):
    counts = {key: refresh_index(key, full=(key == "china50" and full_china50)) for key in INDEXES}
    counts["bond"] = refresh_bonds()
    counts["valuation"] = rebuild_valuation()
    return counts


if __name__ == "__main__":
    print(refresh_all(full_china50=not INDEXES["china50"][1].exists()))

