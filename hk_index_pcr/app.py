"""HKEX HSI/HTI monthly index-options OI PCR research tool. Stdlib only."""
import argparse
from bisect import bisect_left, bisect_right, insort
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import datetime as dt
import html
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import webbrowser
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('HK_PCR_DATA', str(ROOT / 'data')))
DATA.mkdir(exist_ok=True)
OI_FILE = DATA / 'monthly_oi.csv'
PRICE_FILE = DATA / 'prices.csv'
FIELDS = ['date', 'symbol', 'month', 'call_oi', 'put_oi', 'call_volume', 'put_volume', 'source']
PREFIX = {'HSI': 'hsio', 'HTI': 'htio'}
# The public daily ZIP/HTML archive currently reaches back to late 2025.
# Earlier individual URLs return 404 on the live HKEX site.
HISTORY_START = dt.date(2025, 10, 1)
DTOP_START = dt.date(2021, 9, 27)  # five years before September 2026
DTOP_MISSING_FILE = DATA / 'dtop_unavailable.json'
STATE = {'running': False, 'done': 0, 'pending': 0, 'errors': [],
         'message': '待命', 'next_retry': 0, 'price_message': '待检查价格',
         'dtop_running': False, 'dtop_done': 0, 'dtop_pending': 0,
         'dtop_errors': [], 'dtop_message': '历史持仓待检查'}
LOCK = threading.Lock()


class PreText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'pre':
            self.inside = True

    def handle_endtag(self, tag):
        if tag.lower() == 'pre':
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


def report_url(symbol, date):
    return 'https://www.hkex.com.hk/eng/stat/dmstat/dayrpt/' + PREFIX[symbol] + date.strftime('%y%m%d') + '.htm'


def zip_url(symbol, date):
    return report_url(symbol, date).removesuffix('.htm') + '.zip'


def parse_report(raw, symbol, date, source=None):
    """Read the *last* (Combined) totals on each monthly CALL/PUT summary line."""
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', 'replace')
    if '<pre' in raw.lower():
        parser = PreText()
        parser.feed(raw)
        raw = ''.join(parser.parts)
    raw = html.unescape(raw)
    title = 'HANG SENG INDEX OPTIONS' if symbol == 'HSI' else 'HANG SENG TECH INDEX OPTIONS'
    if title not in raw.upper() or 'MARKET TOTAL' not in raw:
        raise ValueError('不是对应品种的完整港交所月度期权日报')
    dates = re.search(r'(\d{1,2} [A-Z]{3} 20\d{2}),\s*[A-Z]+\s+(\d{1,2} [A-Z]{3} 20\d{2}),\s*[A-Z]+', raw[:5000])
    if not dates or dt.datetime.strptime(dates.group(2), '%d %b %Y').date() != date:
        raise ValueError('日报交易日与指定日期不一致')
    # Some reports have an after-hours segment: the final pipe is Combined.
    # Track the contract month in strike rows preceding each summary.
    month = None
    result = {}
    for line in raw.splitlines():
        strike = re.match(r'^\s*([A-Z]{3}-\d{2})\s+\d+(?:\.\d+)?\s+[CP]\s', line)
        if strike:
            month = strike.group(1)
        summary = re.search(r'\|\s*TOTAL (CALL|PUT)\s+(\d+)\s+(\d+)\s+([+-]?\d+)\s*$', line)
        if summary:
            if not month:
                raise ValueError('找到汇总但找不到对应到期月')
            side = summary.group(1).lower()
            result.setdefault(month, {})[side] = (int(summary.group(2)), int(summary.group(3)))
    if not result or any(set(v) != {'call', 'put'} for v in result.values()):
        raise ValueError('未能完整解析逐月 Call/Put 汇总')
    total_match = re.search(r'MARKET TOTAL\s+(\d+)\s+(\d+)\s+[+-]?\d+', raw)
    if not total_match:
        raise ValueError('缺少报告总计')
    got_vol = sum(v[s][0] for v in result.values() for s in ('call', 'put'))
    got_oi = sum(v[s][1] for v in result.values() for s in ('call', 'put'))
    if (got_vol, got_oi) != tuple(map(int, total_match.groups())):
        raise ValueError(f'月度加总 {got_vol}/{got_oi} 与报告总计 {total_match.group(1)}/{total_match.group(2)} 不符')
    return [dict(date=date.isoformat(), symbol=symbol, month=m,
                 call_oi=v['call'][1], put_oi=v['put'][1],
                 call_volume=v['call'][0], put_volume=v['put'][0],
                 source=source or report_url(symbol, date)) for m, v in result.items()]


def parse_zip_report(raw, symbol, date, source=None):
    """Parse HKEX's CSV-in-ZIP report, checking date and market totals."""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = [n for n in z.namelist() if n.lower().endswith('.csv')]
        if len(names) != 1:
            raise ValueError('压缩包内不是单个日报 CSV')
        content = z.read(names[0]).decode('utf-8-sig', 'replace')
    rows = list(csv.reader(io.StringIO(content)))
    title = 'HANG SENG INDEX OPTIONS' if symbol == 'HSI' else 'HANG SENG TECH INDEX OPTIONS'
    if not rows or title not in ' '.join(rows[0]).upper():
        raise ValueError('期权品种不符')
    dates = re.findall(r'\b(\d{1,2} [A-Z]{3} 20\d{2}),\s*(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY)\b',
                       '\n'.join(','.join(r) for r in rows[:35]))
    if len(dates) < 2 or dt.datetime.strptime(dates[-1], '%d %b %Y').date() != date:
        raise ValueError('日报交易日期不符')
    month = None
    totals = {}
    market = None
    for row in rows:
        if len(row) < 20:
            continue
        if re.fullmatch(r'[A-Z]{3}-\d{2}', row[0].strip()) and row[2].strip() in ('C', 'P'):
            month = row[0].strip()
        label = row[15].strip()
        if label in ('TOTAL CALL', 'TOTAL PUT'):
            if month is None:
                raise ValueError('月度汇总缺少对应到期月')
            totals.setdefault(month, {})[label.split()[1].lower()] = (int(row[17]), int(row[18]))
        elif label == 'MARKET TOTAL':
            market = (int(row[17]), int(row[18]))
    if market is None or not totals or any(set(v) != {'call', 'put'} for v in totals.values()):
        raise ValueError('期权日报汇总不完整')
    got = (sum(v[side][0] for v in totals.values() for side in ('call', 'put')),
           sum(v[side][1] for v in totals.values() for side in ('call', 'put')))
    if got != market:
        raise ValueError(f'月度汇总 {got} 与港交所日报总计 {market} 不符')
    return [dict(date=date.isoformat(), symbol=symbol, month=m,
                 call_oi=v['call'][1], put_oi=v['put'][1],
                 call_volume=v['call'][0], put_volume=v['put'][0],
                 source=source or zip_url(symbol, date)) for m, v in totals.items()]


def dtop_url(date):
    return ('https://www.hkex.com.hk/eng/stat/dmstat/oi/'
            f'DTOP_F_{date:%Y%m%d}.zip')


def parse_dtop_zip(raw, date):
    """HKEX clearing gross OI for standard HSI/HTI index options only."""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = [n for n in z.namelist() if n.endswith('_hkcc_opt_dtl_all.raw')]
        if len(names) != 1:
            raise ValueError('缺少唯一的 HKCC 期权逐合约原始文件')
        rows = list(csv.reader(io.StringIO(z.read(names[0]).decode('utf-8-sig', 'replace'))))
    if (len(rows) < 3 or rows[0][:4] != ['H', 'DTOP', 'DCASS', date.strftime('%Y%m%d')]
            or rows[-1][:1] != ['T'] or rows[-1][-1:] != ['EOF']
            or int(rows[-1][1]) != len(rows)-2
            or any(len(r) != 22 or r[0] != '01' for r in rows[1:-1])):
        raise ValueError('历史结算文件的日期、记录数或格式不符')
    grouped = {}
    markets = {('HSI', 'HSI'): 'HSI', ('PDTB6', 'HTI'): 'HTI'}
    for r in rows[1:-1]:
        symbol = markets.get((r[1], r[3]))
        if not symbol:
            continue  # mini, weekly and futures-options contracts are separate
        month = r[5] + '-' + r[6]
        if not re.fullmatch(r'[A-Z]{3}-\d{2}', month):
            raise ValueError('合约到期月份格式不符')
        key = (symbol, month)
        totals = grouped.setdefault(key, [0, 0, 0, 0])
        for j, column in enumerate((8, 15, 11, 18)):
            amount = int(r[column])
            if amount < 0:
                raise ValueError('负的持仓量或成交量')
            totals[j] += amount
    if set(symbol for symbol, _ in grouped) != {'HSI', 'HTI'}:
        raise ValueError('历史结算文件缺少恒指或恒科标准月度期权')
    return [dict(date=date.isoformat(), symbol=symbol, month=month,
                 call_oi=v[0], put_oi=v[1], call_volume=v[2], put_volume=v[3],
                 source=dtop_url(date)) for (symbol, month), v in sorted(grouped.items())]


def price_closes():
    if not PRICE_FILE.exists():
        return {}
    with PRICE_FILE.open(newline='', encoding='utf-8-sig') as f:
        return {(r['date'], r['symbol']): float(r['close']) for r in csv.DictReader(f)
                if r.get('close')}


def read_rows():
    if not OI_FILE.exists():
        return []
    with OI_FILE.open(newline='', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def save_rows(incoming):
    with LOCK:
        old = {(r['date'], r['symbol'], r['month']): r for r in read_rows()}
        for row in incoming:
            old[row['date'], row['symbol'], row['month']] = row
        temp = OI_FILE.with_suffix('.csv.tmp')
        with temp.open('w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, FIELDS)
            writer.writeheader()
            writer.writerows(old[k] for k in sorted(old))
        temp.replace(OI_FILE)


def backfill_dtop():
    """Resume historic HKEX clearing ZIPs, without storing large raw archives."""
    STATE.update(dtop_running=True, dtop_done=0, dtop_errors=[],
                 dtop_message='正在补五年官方持仓历史')
    try:
        existing = {(r['date'], r['symbol'], r['month']): r for r in read_rows()}
        complete = {(r['date'], r['symbol']) for r in existing.values()}
        missing = set(json.loads(DTOP_MISSING_FILE.read_text())) if DTOP_MISSING_FILE.exists() else set()
        calendar = {day for (day, symbol) in price_closes() if symbol == 'HSI'}
        calendar_start = min(calendar) if calendar else None
        end = min(dt.datetime.now(ZoneInfo('Asia/Hong_Kong')).date(), HISTORY_START)
        dates = [DTOP_START + dt.timedelta(days=i) for i in range((end-DTOP_START).days+1)]
        dates = [d for d in dates if d.weekday() < 5 and d.isoformat() not in missing
                 and (not calendar or d.isoformat() < calendar_start
                      or d.isoformat() in calendar)
                 and any((d.isoformat(), s) not in complete for s in PREFIX)]
        dates.reverse()  # newest first: immediately extend the existing series
        STATE['dtop_pending'] = len(dates)

        def fetch(date):
            for attempt in range(3):
                try:
                    url = dtop_url(date)
                    if attempt < 2:
                        request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(request, timeout=24) as response:
                            raw = response.read()
                    else:
                        raw = download(url)
                    return parse_dtop_zip(raw, date)
                except urllib.error.HTTPError as exc:
                    if exc.code == 404 or attempt == 2:
                        raise
                except (urllib.error.URLError, subprocess.TimeoutExpired,
                        zipfile.BadZipFile):
                    if attempt == 2:
                        raise
                time.sleep(attempt + 1)

        with ThreadPoolExecutor(max_workers=3) as pool:
            for pos in range(0, len(dates), 24):
                futures = {pool.submit(fetch, date): date for date in dates[pos:pos+24]}
                new_rows = []
                blocked = False
                for future in as_completed(futures):
                    date = futures[future]
                    try:
                        rows = future.result()
                        for row in rows:
                            key = (row['date'], row['symbol'], row['month'])
                            prior = existing.get(key)
                            if prior and any(int(prior[k]) != int(row[k]) for k in
                                             ('call_oi', 'put_oi', 'call_volume', 'put_volume')):
                                raise ValueError('历史结算持仓与现有港交所日报不一致')
                        new_rows.extend(row for row in rows if
                                        (row['date'], row['symbol'], row['month']) not in existing)
                        STATE['dtop_done'] += 1
                    except urllib.error.HTTPError as e:
                        if e.code == 404:
                            missing.add(date.isoformat())
                        else:
                            STATE['dtop_errors'].append(f'{date}: HTTP {e.code}')
                            if e.code in (403, 429, 503):
                                blocked = True
                    except Exception as e:
                        STATE['dtop_errors'].append(f'{date}: {str(e)[:100]}')
                    STATE['dtop_pending'] -= 1
                if new_rows:
                    save_rows(new_rows)
                    for row in new_rows:
                        existing[row['date'], row['symbol'], row['month']] = row
                if missing:
                    temp = DTOP_MISSING_FILE.with_suffix('.tmp')
                    temp.write_text(json.dumps(sorted(missing)), encoding='utf-8')
                    temp.replace(DTOP_MISSING_FILE)
                if blocked:
                    STATE['dtop_message'] = '港交所暂时限流；已保存进度，下次启动继续'
                    return
        STATE['dtop_message'] = '五年历史补齐检查完成' if not STATE['dtop_errors'] else '部分历史文件无法核对，稍后可重试'
    finally:
        STATE['dtop_running'] = False


def start_dtop_backfill():
    if STATE['dtop_running']:
        return False
    STATE['dtop_running'] = True
    threading.Thread(target=backfill_dtop, daemon=True).start()
    return True


def fetch_report(symbol, date):
    headers = {'User-Agent': 'Mozilla/5.0'}
    url = zip_url(symbol, date)
    try:
        return parse_zip_report(download(url), symbol, date, url)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    # Old report dates can lack a ZIP. Use the HTML report only for a real 404.
    url = report_url(symbol, date)
    return parse_report(download(url), symbol, date, url)


def download(url):
    """Use the system curl client when present; some HKEX ZIPs reject urllib."""
    curl = shutil.which('curl') or shutil.which('curl.exe')
    if curl:
        result = subprocess.run([curl, '--silent', '--show-error', '--location', '--max-time', '18',
                                 '--write-out', '\n__HK_STATUS__%{http_code}', url],
                                capture_output=True, timeout=23)
        body, marker, code = result.stdout.rpartition(b'\n__HK_STATUS__')
        if not marker or not code.isdigit():
            raise urllib.error.URLError(result.stderr.decode('utf-8', 'replace')[:160])
        status = int(code)
        if status != 200:
            raise urllib.error.HTTPError(url, status, 'Data source HTTP response', None, None)
        return body
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=18) as response:
        return response.read()


def backfill(start, end):
    STATE.update(running=True, done=0, errors=[], message='正在自动补齐港交所逐日报表')
    try:
        cached = {(r['date'], r['symbol']) for r in read_rows()}
        tasks = [(day, symbol) for day in (end - dt.timedelta(days=i) for i in range((end-start).days+1))
                 if day.weekday() < 5 for symbol in PREFIX if (day.isoformat(), symbol) not in cached]
        STATE['pending'] = len(tasks)
        failed = 0
        # Four bounded requests in parallel; persist each batch so restarting resumes.
        with ThreadPoolExecutor(max_workers=4) as pool:
            for pos in range(0, len(tasks), 12):
                chunk = tasks[pos:pos+12]
                futures = {pool.submit(fetch_report, symbol, day): (day, symbol) for day, symbol in chunk}
                new_rows = []
                blocked = False
                for future in as_completed(futures):
                    day, symbol = futures[future]
                    try:
                        new_rows.extend(future.result())
                        STATE['done'] += 1
                        failed = 0
                    except urllib.error.HTTPError as e:
                        # A historical weekday can be an HKEX holiday.
                        if e.code != 404:
                            STATE['errors'].append(f'{day} {symbol}: HTTP {e.code}')
                        if e.code in (403, 429, 503):
                            blocked = True
                        elif e.code != 404:
                            failed += 1
                    except (urllib.error.URLError, TimeoutError, ValueError, zipfile.BadZipFile) as e:
                        STATE['errors'].append(f'{day} {symbol}: {str(e)[:90]}')
                        failed += 1
                    STATE['pending'] -= 1
                if new_rows:
                    save_rows(new_rows)
                if blocked or failed >= 8:
                    STATE['message'] = '港交所访问受限，自动补齐已暂停；稍后可重试'
                    STATE['next_retry'] = time.time() + 6*3600
                    return
        STATE['message'] = '本轮历史检查完成' if not STATE['errors'] else '本轮完成；有缺日报表，请看错误记录'
    finally:
        STATE['running'] = False


def start_backfill(start=None, end=None):
    if STATE['running']:
        return False
    today = dt.datetime.now(ZoneInfo('Asia/Hong_Kong')).date()
    STATE['running'] = True
    threading.Thread(target=backfill, args=(start or HISTORY_START, end or today), daemon=True).start()
    return True


def auto_update_loop():
    """Catch new trading days while the local program remains open."""
    last_check = None
    while True:
        time.sleep(15*60)
        now = dt.datetime.now(ZoneInfo('Asia/Hong_Kong'))
        if (now.hour >= 18 and now.weekday() < 5 and last_check != now.date()
                and time.time() >= STATE['next_retry'] and start_backfill(HISTORY_START, now.date())):
            last_check = now.date()
            threading.Thread(target=update_prices, daemon=True).start()


def update_prices():
    """Refresh both cash indexes' daily history from public chart data."""
    incoming = []
    problems = []
    try:
        start = int(dt.datetime.combine(HISTORY_START, dt.time(), dt.timezone.utc).timestamp())
        end = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).timestamp())
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/%5EHSI?period1={start}&period2={end}&interval=1d'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=20) as response:
            data = json.load(response)['chart']['result'][0]
        q = data['indicators']['quote'][0]
        for stamp, opening, closing in zip(data['timestamp'], q['open'], q['close']):
            if opening is None or closing is None:
                continue
            day = dt.datetime.fromtimestamp(stamp, ZoneInfo('Asia/Hong_Kong')).date()
            incoming.append(dict(date=day.isoformat(), symbol='HSI', open=round(opening, 2),
                                 close=round(closing, 2), source=url))
    except (urllib.error.URLError, ValueError, KeyError, IndexError, TypeError, TimeoutError) as e:
        problems.append(f'恒指历史价格读取失败：{str(e)[:100]}')
    try:
        url = ('https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=124.HSTECH'
               '&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'
               f'&klt=101&fqt=0&beg={HISTORY_START:%Y%m%d}&end={dt.date.today():%Y%m%d}')
        data = json.loads(download(url))['data']
        if data['code'] != 'HSTECH' or not data['klines']:
            raise ValueError('恒科历史日线缺失或品种不符')
        for line in data['klines']:
            fields = line.split(',')
            day = dt.date.fromisoformat(fields[0])
            incoming.append(dict(date=day.isoformat(), symbol='HTI', open=float(fields[1]),
                                 close=float(fields[2]), source=url))
    except (urllib.error.URLError, ValueError, KeyError, TypeError, TimeoutError) as e:
        problems.append(f'恒科历史价格读取失败：{str(e)[:100]}')
    if incoming:
        save_prices(incoming, problems)
    STATE['price_message'] = (f'价格已检查：新增 {len(incoming)} 个候选日' +
                              (f'；{"；".join(problems)}' if problems else ''))


def save_prices(incoming, problems):
    old = {}
    if PRICE_FILE.exists():
        with PRICE_FILE.open(newline='', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                old[row['date'], row['symbol']] = row
    for row in incoming:
        key = (row['date'], row['symbol'])
        if key in old:
            try:
                if abs(float(old[key]['close']) / float(row['close']) - 1) > .005:
                    problems.append(f'{key} 两价格源差异大于 0.5%，保留既有数据')
            except (ValueError, ZeroDivisionError, KeyError):
                pass
            continue
        old[key] = row
    temp = PRICE_FILE.with_suffix('.csv.tmp')
    with temp.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, ['date', 'symbol', 'open', 'close', 'source'])
        writer.writeheader()
        writer.writerows(old[key] for key in sorted(old))
    temp.replace(PRICE_FILE)


def last_business_days(year, month):
    if month == 12:
        last = dt.date(year + 1, 1, 1) - dt.timedelta(days=1)
    else:
        last = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    days = []
    while len(days) < 2:
        if last.weekday() < 5:
            days.append(last)
        last -= dt.timedelta(days=1)
    return days[1]  # proxy for actual HKEX penultimate trading day; holidays differ


def expiry_window(year, month, market_dates):
    """Actual HSI sessions for completed months; weekday proxy for future dates."""
    sessions = sorted(dt.date.fromisoformat(x) for x in market_dates
                      if x.startswith(f'{year:04d}-{month:02d}-'))
    next_month = (dt.date(year+1, 1, 1) if month == 12 else dt.date(year, month+1, 1))
    if len(sessions) >= 3 and max(market_dates) >= (next_month-dt.timedelta(days=1)).isoformat():
        return sessions[-3], sessions[-2]
    expiry = last_business_days(year, month)
    previous = expiry-dt.timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= dt.timedelta(days=1)
    return previous, expiry


def series():
    prices = price_closes()
    # HSI cash sessions identify real HK trading days across public holidays.
    market_dates = {day for (day, symbol) in prices if symbol == 'HSI'}
    calendar_start = min(market_dates) if market_dates else None
    calendar_end = max(market_dates) if market_dates else None
    groups = {}
    for r in read_rows():
        try:
            if (calendar_start and calendar_start <= r['date'] <= calendar_end
                    and r['date'] not in market_dates):
                continue
            key = (r['date'], r['symbol'])
            groups.setdefault(key, []).append(dict(month=r['month'], call=int(r['call_oi']), put=int(r['put_oi'])))
        except (KeyError, ValueError):
            continue
    out = []
    for (date, symbol), months in sorted(groups.items()):
        d = dt.date.fromisoformat(date)
        months.sort(key=lambda v: dt.datetime.strptime(v['month'], '%b-%y'))
        active = [m for m in months if m['call'] + m['put'] > 0]
        if not active:
            continue
        front = active[0]
        first = dt.datetime.strptime(front['month'], '%b-%y')
        previous, expiry = expiry_window(first.year, first.month, market_dates)
        near_expiry = d >= expiry - dt.timedelta(days=7)
        last_two = previous <= d <= expiry
        def totals(items):
            c = sum(x['call'] for x in items)
            p = sum(x['put'] for x in items)
            return p, c
        baseline_put, baseline_call = totals(active)
        point = dict(date=date, symbol=symbol, front_month=front['month'],
                     expiry_proxy=near_expiry, expiry_two_days=last_two,
                     put_oi=baseline_put, call_oi=baseline_call,
                     front_oi_share=round((front['put']+front['call']) /
                                          (baseline_put+baseline_call), 6),
                     close=prices.get((date, symbol)))
        for key, items in (('all', active), ('front', [front])):
            p, c = totals(items)
            point[key] = round(p / c, 6) if c else None
            point[key+'_put_oi'], point[key+'_call_oi'] = p, c
        out.append(point)
    for symbol in PREFIX:
        subset = [x for x in out if x['symbol'] == symbol]
        for i, x in enumerate(subset):
            x['front_roll'] = i > 0 and subset[i-1]['front_month'] != x['front_month']
        for key in ('all', 'front'):
            # Keep five calendar years of PCR, including today's value. The
            # sorted copy lets each new date update its rank incrementally.
            history = deque()
            sorted_values = []
            for i, x in enumerate(subset):
                window = subset[max(0, i-19):i+1]
                values = [v[key] for v in window]
                # Fail closed on missing weekdays. Official HK holidays may also
                # block a band until a holiday calendar is provided.
                missing = False
                for prev, nxt in zip(window, window[1:]):
                    d = dt.date.fromisoformat(prev['date']) + dt.timedelta(days=1)
                    stop = dt.date.fromisoformat(nxt['date'])
                    while d < stop:
                        if d.isoformat() in market_dates or (not market_dates and d.weekday() < 5):
                            missing = True
                        d += dt.timedelta(days=1)
                if len(values) == 20 and None not in values and not missing:
                    mean = statistics.mean(values)
                    sd = statistics.pstdev(values)
                    x[key + '_ma'] = round(mean, 6)
                    x[key + '_lower'] = round(mean - 2 * sd, 6)
                    x[key + '_upper'] = round(mean + 2 * sd, 6)
                    x[key + '_below'] = values[-1] < mean - 2 * sd
                    if key == 'front' and (x['expiry_proxy'] or x['front_roll']):
                        x['front_expiry_warning'] = True
                current_date = dt.date.fromisoformat(x['date'])
                try:
                    cutoff = current_date.replace(year=current_date.year - 5)
                except ValueError:  # leap day
                    cutoff = current_date.replace(year=current_date.year - 5, day=28)
                while history and history[0][0] < cutoff:
                    _, expired = history.popleft()
                    sorted_values.pop(bisect_left(sorted_values, expired))
                if x[key] is not None:
                    history.append((current_date, x[key]))
                    insort(sorted_values, x[key])
                x[key + '_percentile_count'] = len(history)
                x[key + '_percentile_start'] = history[0][0].isoformat() if history else None
                first_possible = cutoff
                while first_possible.weekday() >= 5:
                    first_possible += dt.timedelta(days=1)
                x[key + '_percentile_spans_five_years'] = bool(
                    history and dt.date.fromisoformat(subset[0]['date']) <= first_possible)
                if len(history) >= 20 and x[key] is not None:
                    x[key + '_percentile'] = round(
                        100 * bisect_right(sorted_values, x[key]) / len(sorted_values), 2)
    return out


class Handler(BaseHTTPRequestHandler):
    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        if url.path == '/api/data':
            self.send_json({'series': series(), 'state': STATE})
        elif url.path == '/api/backfill':
            try:
                start = dt.date.fromisoformat(q.get('start', [HISTORY_START.isoformat()])[0])
                end = dt.date.fromisoformat(q.get('end', [dt.datetime.now(ZoneInfo('Asia/Hong_Kong')).date().isoformat()])[0])
                if end < start or (end - start).days > 365*5:
                    raise ValueError('日期范围须在五年以内')
            except ValueError as e:
                self.send_json({'error': str(e)}, 400)
                return
            if not start_backfill(start, end):
                self.send_json({'error': '任务正在运行'}, 409)
                return
            self.send_json({'started': True})
        elif url.path == '/':
            data = (ROOT / 'dashboard.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path != '/api/import':
            self.send_error(404)
            return
        try:
            body = self.rfile.read(min(int(self.headers.get('Content-Length', '0')), 2_000_000))
            req = json.loads(body)
            date = dt.date.fromisoformat(req['date'])
            symbol = req['symbol']
            if symbol not in PREFIX:
                raise ValueError('品种须为 HSI 或 HTI')
            rows = parse_report(req['text'], symbol, date)
            save_rows(rows)
            self.send_json({'months': len(rows), 'date': date.isoformat(), 'symbol': symbol})
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            self.send_json({'error': str(e)}, 400)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', nargs='?', default='serve', choices=('serve', 'backfill'))
    parser.add_argument('--start', default=(dt.date.today() - dt.timedelta(days=45)).isoformat())
    parser.add_argument('--end', default=dt.date.today().isoformat())
    args = parser.parse_args()
    if args.command == 'backfill':
        backfill(dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end))
        print(json.dumps(STATE, ensure_ascii=False, indent=2))
    else:
        server = ThreadingHTTPServer(('127.0.0.1', 8068), Handler)
        print('恒指 / 恒科 OI-PCR: http://127.0.0.1:8068/')
        start_backfill()
        threading.Thread(target=update_prices, daemon=True).start()
        threading.Thread(target=auto_update_loop, daemon=True).start()
        threading.Timer(0.8, lambda: webbrowser.open('http://127.0.0.1:8068/')).start()
        server.serve_forever()

