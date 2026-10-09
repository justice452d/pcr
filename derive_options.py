"""Build compact all-contract/front-month PCR series from the shared H:\\PCR cache."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import statistics
import threading
import zipfile
from datetime import datetime
from pathlib import Path


PCR_HOME = Path(os.environ.get("PCR_HOME", r"H:\PCR"))
CACHE = PCR_HOME / "cache"
OUTPUT = CACHE / "derived_options_v1.json"
LOCK = threading.Lock()
PRODUCTS = {
    "au": ("沪金", "黄金期权"),
    "ag": ("沪银", "白银期权"),
    "cu": ("沪铜", "铜期权"),
    "sc": ("原油", "原油期权"),
}


def number(value):
    try:
        text = str(value).replace(",", "").strip()
        return None if text in ("", "-", "--", "None", "nan") else float(text)
    except (TypeError, ValueError):
        return None


def read_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def futures_by_product(day: str):
    path = CACHE / f"fu_{day}.json"
    if not path.exists():
        return {}
    result = {}
    try:
        rows = read_json(path).get("o_curinstrument", [])
    except (OSError, ValueError):
        return result
    for code in PRODUCTS:
        choices = []
        for row in rows:
            inst = str(row.get("INSTRUMENTID") or "").replace(" ", "").upper()
            product_id = str(row.get("PRODUCTID") or "").strip().lower()
            if not inst and product_id in (code, code + "_f"):
                month = str(row.get("DELIVERYMONTH") or "").strip()
                if re.fullmatch(r"\d{4}", month):
                    inst = code.upper() + month
            if not re.fullmatch(rf"{code.upper()}\d{{4}}", inst):
                continue
            oi, close = number(row.get("OPENINTEREST")), number(row.get("CLOSEPRICE"))
            if oi is not None and close is not None:
                choices.append((oi, close, inst))
        if choices:
            oi, close, inst = max(choices)
            result[code] = {"close": close, "contract": inst, "future_oi": oi}
    return result


def parse_shfe_day(path: Path):
    day = path.stem.split("_", 1)[1]
    obj = read_json(path)
    rows = obj.get("o_curinstrument", [])
    prices = futures_by_product(day)
    grouped = {code: {} for code in PRODUCTS}
    for row in rows:
        inst = str(row.get("INSTRUMENTID") or "").replace(" ", "").upper()
        match = re.match(r"^(AU|AG|CU|SC)(\d{4})-?([CP])-?\d", inst)
        if not match:
            continue
        code, month, side = match.groups()
        code = code.lower()
        oi = number(row.get("OPENINTEREST"))
        if code not in grouped or oi is None or oi < 0:
            continue
        bucket = grouped[code].setdefault(month, {"put": 0.0, "call": 0.0})
        bucket["put" if side == "P" else "call"] += oi
    result = {}
    date = datetime.strptime(day, "%Y%m%d").date().isoformat()
    for code, months in grouped.items():
        active = [(month, values) for month, values in sorted(months.items())
                  if values["put"] > 0 and values["call"] > 0]
        if not active:
            continue
        front_month, front = active[0]
        put = sum(values["put"] for _, values in active)
        call = sum(values["call"] for _, values in active)
        row = {
            "date": date,
            "all": put / call,
            "all_put_oi": round(put),
            "all_call_oi": round(call),
            "front": front["put"] / front["call"],
            "front_put_oi": round(front["put"]),
            "front_call_oi": round(front["call"]),
            "front_month": code.upper() + front_month,
            "front_oi_share": (front["put"] + front["call"]) / (put + call),
        }
        row.update(prices.get(code, {}))
        result[code] = row
    return result


def decode_csv(raw: bytes):
    for encoding in ("gb18030", "gbk", "utf-8-sig", "utf-8"):
        try:
            text = raw.decode(encoding)
            if "合约" in text:
                return text
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "ignore")


def pick(row, *keys):
    for key in keys:
        if key in row and str(row[key]).strip():
            return row[key]
    return None


def parse_cffex_csv(day: str, raw: bytes, option_code: str, future_code: str):
    option_code = option_code.upper()
    future_code = future_code.upper()
    reader = csv.DictReader(io.StringIO(decode_csv(raw)))
    groups = {}
    future = []
    for raw_row in reader:
        row = {(str(key).replace("\ufeff", "").strip() if key else ""):
               (value.strip() if isinstance(value, str) else value)
               for key, value in raw_row.items()}
        symbol = str(pick(row, "合约代码", "合约", "instrumentId") or "").replace(" ", "").upper()
        oi = number(pick(row, "持仓量", "空盘量"))
        match = re.match(rf"^{re.escape(option_code)}(\d{{4}})[-_]?([CP])[-_]", symbol)
        if match and oi is not None:
            month, side = match.groups()
            bucket = groups.setdefault(month, {"put": 0.0, "call": 0.0})
            bucket["put" if side == "P" else "call"] += oi
        elif re.fullmatch(rf"{re.escape(future_code)}\d{{4}}", symbol):
            close = number(pick(row, "今收盘", "收盘价", "收盘"))
            if oi is not None and close is not None:
                future.append((oi, close, symbol))
    active = [(month, values) for month, values in sorted(groups.items())
              if values["put"] > 0 and values["call"] > 0]
    if not active:
        return None
    front_month, front = active[0]
    put = sum(values["put"] for _, values in active)
    call = sum(values["call"] for _, values in active)
    out = {
        "date": datetime.strptime(day, "%Y%m%d").date().isoformat(),
        "all": put / call,
        "all_put_oi": round(put),
        "all_call_oi": round(call),
        "front": front["put"] / front["call"],
        "front_put_oi": round(front["put"]),
        "front_call_oi": round(front["call"]),
        "front_month": option_code + front_month,
        "front_oi_share": (front["put"] + front["call"]) / (put + call),
    }
    if future:
        oi, close, symbol = max(future)
        out.update(close=close, contract=symbol, future_oi=oi)
    return out


def parse_io_csv(day: str, raw: bytes):
    """Backward-compatible IO parser used by older tests and scripts."""
    return parse_cffex_csv(day, raw, "IO", "IF")


def parse_ho_csv(day: str, raw: bytes):
    return parse_cffex_csv(day, raw, "HO", "IH")


def scan_cffex(existing_dates, option_code: str, future_code: str):
    rows = []
    cffex = CACHE / "cffex"
    for path in sorted(cffex.glob("20????.zip")):
        try:
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    match = re.search(r"(20\d{6})_1\.csv$", name)
                    if not match or match.group(1) in existing_dates:
                        continue
                    row = parse_cffex_csv(match.group(1), archive.read(name), option_code, future_code)
                    if row:
                        rows.append(row)
        except (OSError, ValueError, zipfile.BadZipFile):
            continue
    return rows


def add_bands(rows):
    rows.sort(key=lambda row: row["date"])
    for mode in ("all", "front"):
        for index, row in enumerate(rows):
            values = [item.get(mode) for item in rows[max(0, index - 19):index + 1]]
            if len(values) == 20 and None not in values:
                avg = statistics.fmean(values)
                sd = statistics.pstdev(values)
                row[mode + "_ma"] = avg
                row[mode + "_upper"] = avg + 2 * sd
                row[mode + "_lower"] = avg - 2 * sd
            else:
                row[mode + "_ma"] = row[mode + "_upper"] = row[mode + "_lower"] = None


def build(force=False):
    """Incrementally process only cache files not already in the compact archive."""
    with LOCK:
        data = {key: [] for key in ("io", "ho", *PRODUCTS)}
        if OUTPUT.exists() and not force:
            try:
                cached = read_json(OUTPUT)
                for key in data:
                    data[key] = cached.get(key, [])
            except (OSError, ValueError):
                pass
        known = {key: {row["date"] for row in rows} for key, rows in data.items()}
        known_shfe_days = set.intersection(*(known[key] for key in PRODUCTS)) if any(data[key] for key in PRODUCTS) else set()
        for path in sorted(CACHE.glob("op_20??????.json")):
            iso = datetime.strptime(path.stem.split("_", 1)[1], "%Y%m%d").date().isoformat()
            if iso in known_shfe_days:
                continue
            try:
                parsed = parse_shfe_day(path)
            except (OSError, ValueError, KeyError):
                continue
            for key, row in parsed.items():
                if row["date"] not in known[key]:
                    data[key].append(row)
                    known[key].add(row["date"])
        data["io"].extend(scan_cffex({date.replace("-", "") for date in known["io"]}, "IO", "IF"))
        data["ho"].extend(scan_cffex({date.replace("-", "") for date in known["ho"]}, "HO", "IH"))
        for rows in data.values():
            unique = {row["date"]: row for row in rows}
            rows[:] = list(unique.values())
            add_bands(rows)
        temporary = OUTPUT.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(OUTPUT)
        return data


def load():
    if not OUTPUT.exists():
        return build()
    try:
        return read_json(OUTPUT)
    except (OSError, ValueError):
        return build(force=True)


if __name__ == "__main__":
    result = build()
    print({key: (len(rows), rows[-1]["date"] if rows else None) for key, rows in result.items()})

