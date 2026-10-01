"""Cboe SPX/NDX open-interest PCR snapshots with persistent daily history."""
from __future__ import annotations

import json
import os
import ssl
import statistics
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


PCR_HOME = Path(os.environ.get("PCR_HOME", r"H:\PCR"))
CACHE = PCR_HOME / "cache" / "cboe_index_pcr.json"
URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/_{}.json"
SYMBOLS = {"spx": "SPX", "ndx": "NDX"}
LOCK = threading.Lock()


def _download(symbol):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.cboe.com/",
        "Connection": "close",
    }
    request = urllib.request.Request(URL.format(symbol), headers=headers)
    errors = []
    for attempt in range(3):
        for context in (ssl.create_default_context(), ssl._create_unverified_context()):
            try:
                with urllib.request.urlopen(request, context=context, timeout=75) as response:
                    payload = json.load(response)
                if not isinstance(payload.get("data", {}).get("options"), list):
                    raise ValueError("Cboe 返回中没有期权链")
                return payload
            except Exception as exc:
                errors.append(str(exc))
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("Cboe 下载失败：" + " | ".join(errors[-3:]))


def _trading_date(payload):
    dates = Counter()
    for row in payload["data"]["options"]:
        if (row.get("volume") or 0) > 0 and row.get("last_trade_time"):
            dates[str(row["last_trade_time"])[:10]] += 1
    if dates:
        return dates.most_common(1)[0][0]
    return str(payload.get("timestamp") or "")[:10]


def _snapshot(key, payload):
    day = _trading_date(payload)
    if len(day) != 10:
        raise ValueError("无法识别 Cboe 数据日期")
    cutoff = day[2:].replace("-", "")
    expiries = defaultdict(lambda: {"call": 0.0, "put": 0.0})
    contracts = 0
    for row in payload["data"]["options"]:
        name = str(row.get("option") or "")
        if len(name) < 15:
            continue
        tail = name[-15:]
        expiry, side = tail[:6], tail[6]
        if not expiry.isdigit() or expiry < cutoff or side not in ("C", "P"):
            continue
        try:
            oi = float(row.get("open_interest") or 0)
        except (TypeError, ValueError):
            continue
        if oi < 0:
            continue
        expiries[expiry]["put" if side == "P" else "call"] += oi
        contracts += 1
    active = [(expiry, values) for expiry, values in sorted(expiries.items())
              if values["put"] > 0 and values["call"] > 0]
    if not active:
        raise ValueError(f"{SYMBOLS[key]} 没有同时具备 Put/Call OI 的有效到期日")
    front_expiry, front = active[0]
    put = sum(values["put"] for _, values in active)
    call = sum(values["call"] for _, values in active)
    front_date = datetime.strptime("20" + front_expiry, "%Y%m%d").date().isoformat()
    return {
        "date": day,
        "all": put / call,
        "all_put_oi": round(put),
        "all_call_oi": round(call),
        "front": front["put"] / front["call"],
        "front_put_oi": round(front["put"]),
        "front_call_oi": round(front["call"]),
        "front_month": front_date,
        "front_oi_share": (front["put"] + front["call"]) / (put + call),
        "close": payload["data"].get("current_price"),
        "contracts": contracts,
        "source_timestamp": payload.get("timestamp"),
        "source": "Cboe delayed options quotes",
    }


def _bands(rows):
    rows.sort(key=lambda row: row["date"])
    for mode in ("all", "front"):
        for index, row in enumerate(rows):
            values = [item.get(mode) for item in rows[max(0, index - 19):index + 1]]
            if len(values) == 20 and None not in values:
                avg, sd = statistics.fmean(values), statistics.pstdev(values)
                row[mode + "_ma"] = avg
                row[mode + "_upper"] = avg + 2 * sd
                row[mode + "_lower"] = avg - 2 * sd
            else:
                row[mode + "_ma"] = row[mode + "_upper"] = row[mode + "_lower"] = None


def load():
    if CACHE.exists():
        try:
            data = json.loads(CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    else:
        data = {}
    result = {key: list(data.get(key, [])) for key in SYMBOLS}
    for rows in result.values():
        _bands(rows)
    return result


def refresh(symbols=None):
    with LOCK:
        data = load()
        errors = {}
        for key in symbols or SYMBOLS:
            try:
                row = _snapshot(key, _download(SYMBOLS[key]))
                by_date = {item["date"]: item for item in data[key]}
                by_date[row["date"]] = row
                data[key] = list(by_date.values())
                _bands(data[key])
            except Exception as exc:
                errors[key] = str(exc)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        temporary = CACHE.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(CACHE)
        return data, errors


if __name__ == "__main__":
    series, problems = refresh()
    print({key: rows[-1] if rows else None for key, rows in series.items()})
    print("errors", problems)

