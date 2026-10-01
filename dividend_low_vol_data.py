"""Data layer for the H30269 dividend-low-volatility valuation thermometer."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PCR_HOME = Path(os.environ.get("PCR_HOME", r"H:\PCR"))
CACHE_DIR = PCR_HOME / "cache" / "valuation"
CACHE_FILE = CACHE_DIR / "dividend_low_vol_history.json"
MANUAL_JSON = CACHE_DIR / "valuation_manual.json"
MANUAL_CSV = CACHE_DIR / "valuation_manual.csv"
INDEX_CODE = "H30269"
INDEX_NAME = "中证红利低波动指数"
INDICATOR_URL = ("https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
                 "file/autofile/indicator/H30269indicator.xls")
FACTSHEET_URL = ("https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
                 "indices/detail/files/zh_CN/H30269factsheet.pdf")
PERF_URL = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
            "?indexCode=H30269&startDate={}&endDate={}")
MAX_PB_AGE_DAYS = 45

vendor = ROOT / "vendor"
if vendor.exists():
    sys.path.insert(0, str(vendor))

from valuation_models.dividend_low_vol import calculate_series, validate_weights  # noqa: E402


RAW_FIELDS = ("dividend_yield", "pb", "cn10y", "index_price")


def _request(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=35) as response:
        return response.read()


def _number(value):
    if value in (None, "", "null"):
        return None
    try:
        result = float(value)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def _date(raw: str) -> str:
    text = str(raw).strip().replace("/", "-")
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text[:10]).isoformat()


def load_cache() -> list[dict]:
    if not CACHE_FILE.exists():
        return []
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else payload.get("data", [])
    except (OSError, ValueError, TypeError):
        return []


def _manual_rows() -> list[dict]:
    rows = []
    if MANUAL_CSV.exists():
        with MANUAL_CSV.open(newline="", encoding="utf-8-sig") as handle:
            rows.extend(csv.DictReader(handle))
    if MANUAL_JSON.exists():
        payload = json.loads(MANUAL_JSON.read_text(encoding="utf-8-sig"))
        rows.extend(payload if isinstance(payload, list) else [payload])
    output = []
    for source in rows:
        if not source.get("date"):
            continue
        row = {"date": _date(source["date"]), "update_time": datetime.now().isoformat(timespec="seconds")}
        for field in RAW_FIELDS:
            row[field] = _number(source.get(field))
            if row[field] is not None:
                row[field + "_source"] = "手动"
        row["source"] = "手动"
        output.append(row)
    return output


def fetch_indicator_rows() -> list[dict]:
    import xlrd

    book = xlrd.open_workbook(file_contents=_request(INDICATOR_URL))
    sheet = book.sheet_by_index(0)
    rows = []
    for index in range(1, sheet.nrows):
        values = sheet.row_values(index)
        if len(values) < 10:
            continue
        dividend = _number(values[8])
        if dividend is None:
            continue
        rows.append({"date": _date(values[0]), "dividend_yield": dividend,
                     "dividend_yield_source": "中证指数 H30269 指标文件（总股本口径）"})
    return sorted(rows, key=lambda row: row["date"])


def fetch_prices(start: str, end: str) -> dict[str, float]:
    url = PERF_URL.format(start.replace("-", ""), end.replace("-", ""))
    payload = json.loads(_request(url).decode("utf-8"))
    result = {}
    for item in payload.get("data") or []:
        close = _number(item.get("close"))
        if close is not None:
            result[_date(item["tradeDate"])] = close
    return result


def load_bonds() -> dict[str, float]:
    path = ROOT / "sources" / "bond_csi" / "bond_10y_daily.csv"
    result = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            value = _number(row.get("yield10_pct"))
            if value is not None:
                result[_date(row["date"])] = value
    return result


def fetch_factsheet_pb() -> dict:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(_request(FACTSHEET_URL))) as document:
        text = document.pages[0].extract_text(layout=True) or ""
    day_match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text)
    pb_match = re.search(r"市净率\s+([.]?\d+(?:\.\d+)?)", text)
    if not day_match or not pb_match:
        raise ValueError("中证事实表未识别出日期或市净率")
    day = f"{day_match.group(1)}-{int(day_match.group(2)):02d}-{int(day_match.group(3)):02d}"
    return {"date": day, "pb": float(pb_match.group(1)),
            "source": "中证指数 H30269 月度事实表"}


def _merge_field(target: dict, source: dict, field: str, overwrite=False):
    if source.get(field) is None:
        return
    if overwrite or target.get(field) is None:
        target[field] = _number(source[field])
        target[field + "_source"] = source.get(field + "_source") or source.get("source")


def _save(rows: list[dict]):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CACHE_FILE)


def refresh() -> tuple[list[dict], dict]:
    """Refresh available official fields; keep prior verified cache on partial failure."""
    now = datetime.now().isoformat(timespec="seconds")
    merged = {row["date"]: dict(row) for row in _manual_rows()}
    for row in load_cache():
        if row.get("date"):
            current = merged.setdefault(row["date"], {"date": row["date"]})
            for field in RAW_FIELDS:
                _merge_field(current, row, field, overwrite=True)
            for key in ("pb_asof", "update_time"):
                if row.get(key):
                    current[key] = row[key]
    errors = {}
    try:
        indicators = fetch_indicator_rows()
    except Exception as exc:
        indicators = []
        errors["dividend_yield"] = str(exc)
    factsheet = None
    try:
        factsheet = fetch_factsheet_pb()
    except Exception as exc:
        errors["pb"] = str(exc)
    prices = {}
    if indicators:
        try:
            prices = fetch_prices(indicators[0]["date"], indicators[-1]["date"])
        except Exception as exc:
            errors["index_price"] = str(exc)
    try:
        bonds = load_bonds()
    except Exception as exc:
        bonds = {}
        errors["cn10y"] = str(exc)
    for indicator in indicators:
        day = indicator["date"]
        current = merged.setdefault(day, {"date": day})
        _merge_field(current, indicator, "dividend_yield", overwrite=True)
        if day in prices:
            _merge_field(current, {"index_price": prices[day],
                                   "index_price_source": "中证指数 H30269 价格指数"},
                         "index_price", overwrite=True)
        if day in bonds:
            _merge_field(current, {"cn10y": bonds[day],
                                   "cn10y_source": "中国债券信息网中债国债收益率曲线"},
                         "cn10y", overwrite=True)
        if factsheet:
            age = (date.fromisoformat(day) - date.fromisoformat(factsheet["date"])).days
            if 0 <= age <= MAX_PB_AGE_DAYS:
                _merge_field(current, {"pb": factsheet["pb"],
                                       "pb_source": factsheet["source"]}, "pb", overwrite=True)
                current["pb_asof"] = factsheet["date"]
        current["update_time"] = now
        current["source"] = "自动" if all(current.get(field + "_source", "").startswith(("中证", "中国"))
                                               for field in RAW_FIELDS if current.get(field) is not None) else current.get("source", "缓存")
    raw_rows = []
    for day in sorted(merged):
        row = {key: value for key, value in merged[day].items()
               if key in {"date", "source", "update_time", "pb_asof"} or
               key in RAW_FIELDS or key.endswith("_source")}
        raw_rows.append(row)
    calculated = calculate_series(raw_rows, window="10")
    _save(calculated)
    return calculated, errors


def _quantile(values, probability):
    values = sorted(float(value) for value in values if value is not None)
    if not values:
        return None
    position = (len(values) - 1) * probability
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def payload(window="10", weights=None) -> dict:
    weights = validate_weights(weights)
    raw = [{key: row.get(key) for key in row
            if key in {"date", "source", "update_time", "pb_asof"} or
            key in RAW_FIELDS or key.endswith("_source")} for row in load_cache()]
    rows = calculate_series(raw, window=window, weights=weights)
    latest = rows[-1] if rows else None
    stats = {}
    for field in ("dividend_yield", "pb"):
        values = [row.get(field) for row in rows if row.get(field) is not None]
        stats[field] = {"min": min(values) if values else None,
                        "q25": _quantile(values, .25), "median": _quantile(values, .5),
                        "q75": _quantile(values, .75), "max": max(values) if values else None}
    return {
        "index": {"code": INDEX_CODE, "name": INDEX_NAME,
                  "price_type": "价格指数，未包含现金分红再投资"},
        "data": rows,
        "latest": latest,
        "statistics": stats,
        "window": str(window).lower(),
        "weights": weights,
        "cache_file": str(CACHE_FILE),
        "manual_json": str(MANUAL_JSON),
        "manual_csv": str(MANUAL_CSV),
        "is_today": bool(latest and latest["date"] == date.today().isoformat()),
    }

