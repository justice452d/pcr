"""Local-only unified PCR, equity/bond spread, and valuation dashboard."""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import traceback
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
PCR_HOME = Path(os.environ.get("PCR_HOME", r"H:\PCR"))
os.environ.setdefault("HK_PCR_DATA", str(PCR_HOME / "cache" / "hk_index_pcr"))

import cffex_io
import derive_options
import dividend_low_vol_data
import hk_index_pcr.app as hk
import market_data
import shfe_gold
import shfe_others


PORT = int(os.environ.get("PCR_PORT", "8090"))
HOST = "0.0.0.0"
STATE = {
    "started": datetime.now().isoformat(timespec="seconds"),
    "options": "读取公共缓存",
    "hk": "读取五年缓存",
    "market": "读取本地数据",
    "dividend_low_vol": "读取红利低波估值缓存",
    "last_option_refresh": None,
    "last_market_refresh": None,
    "errors": [],
}
DATA_LOCK = threading.Lock()
OPTION_DATA = {}


def local_ip():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        result = sock.getsockname()[0]
        sock.close()
        return result
    except OSError:
        return "127.0.0.1"


def reload_options(force=False):
    global OPTION_DATA
    STATE["options"] = "正在整理裸 PCR / 近月 PCR 缓存"
    data = derive_options.build(force=force)
    with DATA_LOCK:
        OPTION_DATA = data
    latest = max((rows[-1]["date"] for rows in data.values() if rows), default="无")
    STATE["options"] = f"已就绪，最近交易日 {latest}"
    return data


def fetch_latest_options():
    """Ask each official source for its newest close, then rebuild compact rows."""
    errors = []
    for label, task in (
        ("沪金", shfe_gold.latest),
        ("沪银", lambda: shfe_others.latest("silver")),
        ("沪铜", lambda: shfe_others.latest("copper")),
        ("原油", lambda: shfe_others.latest("oil")),
        ("沪深300", cffex_io.latest),
    ):
        try:
            task()
        except Exception as exc:
            errors.append(f"{label}: {str(exc)[:160]}")
    reload_options()
    STATE["last_option_refresh"] = datetime.now().isoformat(timespec="seconds")
    if errors:
        STATE["errors"] = (STATE["errors"] + errors)[-12:]
    return errors


def option_scheduler():
    last_attempt = None
    while True:
        now = datetime.now()
        if now.weekday() < 5 and now.time() >= datetime.strptime("15:30", "%H:%M").time():
            if last_attempt != now.date() or now.minute % 30 < 10:
                try:
                    fetch_latest_options()
                    last_attempt = now.date()
                except Exception as exc:
                    STATE["errors"] = (STATE["errors"] + ["期权更新: " + str(exc)])[-12:]
        time.sleep(600)


def hk_scheduler():
    try:
        hk.update_prices()
        hk.start_backfill(hk.HISTORY_START, date.today())
        hk.start_dtop_backfill()
        STATE["hk"] = "后台更新已启动"
        hk.auto_update_loop()
    except Exception as exc:
        STATE["hk"] = "后台更新失败"
        STATE["errors"] = (STATE["errors"] + ["港股更新: " + str(exc)])[-12:]


def market_scheduler():
    last_day = None
    last_month = None
    while True:
        now = datetime.now()
        should_daily = now.weekday() < 5 and now.hour >= 18 and last_day != now.date()
        should_monthly = now.day <= 3 and last_month != (now.year, now.month)
        if should_daily or should_monthly:
            try:
                STATE["market"] = "正在检查日频与月频数据"
                counts = market_data.refresh_all(full_china50=False)
                _, dividend_errors = dividend_low_vol_data.refresh()
                STATE["market"] = "更新完成：" + "、".join(f"{key} {value}" for key, value in counts.items())
                STATE["dividend_low_vol"] = ("红利低波更新完成" if not dividend_errors else
                                               "红利低波部分数据源失败，继续使用缓存")
                STATE["last_market_refresh"] = now.isoformat(timespec="seconds")
                last_day = now.date()
                last_month = (now.year, now.month)
            except Exception as exc:
                STATE["market"] = "更新失败，继续使用已核实缓存"
                STATE["errors"] = (STATE["errors"] + ["市场更新: " + str(exc)])[-12:]
        time.sleep(900)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path in ("/", "/index.html"):
                return self.send_file(ROOT / "dashboard.html", "text/html; charset=utf-8")
            if parsed.path == "/api/status":
                return self.send_json({**STATE, "lan_url": f"http://{local_ip()}:{PORT}"})
            if parsed.path == "/api/options":
                product = query.get("product", ["io"])[0]
                days = max(20, min(2000, int(query.get("days", ["260"])[0])))
                with DATA_LOCK:
                    rows = list(OPTION_DATA.get(product, []))[-days:]
                return self.send_json({"product": product, "data": rows, "status": STATE["options"]})
            if parsed.path == "/api/hk":
                symbol = query.get("symbol", ["HTI"])[0]
                rows = [row for row in hk.series() if row["symbol"] == symbol]
                return self.send_json({"symbol": symbol, "data": rows, "state": hk.STATE})
            if parsed.path == "/api/market":
                key = query.get("index", ["sse"])[0]
                if key not in market_data.INDEXES:
                    return self.send_json({"error": "未知指数"}, 400)
                return self.send_json({"index": key, "data": market_data.build_market(key)})
            if parsed.path == "/api/valuation":
                return self.send_json({"data": market_data.valuation()})
            if parsed.path == "/api/dividend-low-vol":
                window = query.get("window", ["10"])[0].lower()
                weights = {
                    "dividend": float(query.get("wd", ["45"])[0]),
                    "spread": float(query.get("ws", ["35"])[0]),
                    "pb": float(query.get("wp", ["20"])[0]),
                }
                result = dividend_low_vol_data.payload(window=window, weights=weights)
                result["status"] = STATE["dividend_low_vol"]
                return self.send_json(result)
            if parsed.path == "/api/refresh":
                target = query.get("target", ["options"])[0]
                if target == "options":
                    threading.Thread(target=fetch_latest_options, daemon=True).start()
                elif target == "hk":
                    threading.Thread(target=hk.update_prices, daemon=True).start()
                    hk.start_backfill(hk.HISTORY_START, date.today())
                elif target == "market":
                    threading.Thread(target=self._refresh_market, daemon=True).start()
                elif target == "dividend-low-vol":
                    threading.Thread(target=self._refresh_dividend_low_vol, daemon=True).start()
                else:
                    return self.send_json({"error": "未知更新目标"}, 400)
                return self.send_json({"started": True, "target": target})
            return self.send_json({"error": "Not found"}, 404)
        except Exception as exc:
            traceback.print_exc()
            return self.send_json({"error": str(exc)}, 500)

    @staticmethod
    def _refresh_market():
        try:
            counts = market_data.refresh_all(full_china50=False)
            STATE["market"] = "更新完成：" + "、".join(f"{key} {value}" for key, value in counts.items())
            STATE["last_market_refresh"] = datetime.now().isoformat(timespec="seconds")
        except Exception as exc:
            STATE["errors"] = (STATE["errors"] + ["市场更新: " + str(exc)])[-12:]

    @staticmethod
    def _refresh_dividend_low_vol():
        try:
            STATE["dividend_low_vol"] = "正在更新红利低波估值"
            rows, errors = dividend_low_vol_data.refresh()
            latest = rows[-1]["date"] if rows else "无"
            STATE["dividend_low_vol"] = (f"已就绪，最近数据 {latest}" if not errors else
                                           f"部分数据源失败，显示缓存至 {latest}")
            if errors:
                STATE["errors"] = (STATE["errors"] +
                                   [f"红利低波 {key}: {value}" for key, value in errors.items()])[-12:]
        except Exception as exc:
            STATE["dividend_low_vol"] = "更新失败，继续使用现有缓存"
            STATE["errors"] = (STATE["errors"] + ["红利低波更新: " + str(exc)])[-12:]

def main(open_browser=True):
    global PORT
    reload_options()
    threading.Thread(target=Handler._refresh_dividend_low_vol, daemon=True).start()
    for target in (option_scheduler, hk_scheduler, market_scheduler):
        threading.Thread(target=target, daemon=True).start()
    server = None
    preferred = PORT
    for candidate in range(preferred, 8100):
        try:
            server = ThreadingHTTPServer((HOST, candidate), Handler)
            PORT = candidate
            break
        except OSError:
            continue
    if server is None:
        raise OSError(f"{preferred}-8099 没有可用端口")
    url = f"http://127.0.0.1:{PORT}"
    print("PCR 综合研究台", url, "局域网", f"http://{local_ip()}:{PORT}", flush=True)
    if open_browser:
        threading.Timer(1, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main(open_browser="--no-browser" not in os.sys.argv)

