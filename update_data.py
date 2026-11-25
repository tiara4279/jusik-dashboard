"""
시장 바닥 관제 대시보드 데이터 갱신 스크립트

실행:  python update_data.py
결과:  market_data.js  (index.html이 읽는 데이터)
       market_data.json (같은 내용, 다음 실행 때 실패한 항목의 대체값으로 사용)

자동 수집
  - FRED: 10년물, EFFR, HY/CCC 스프레드, 은행 준비금, 역레포, M2, 나스닥, S&P500
    (FRED_API_KEY 환경변수가 있으면 공식 API 사용. 없으면 CSV → 막히면 NY 연준·Yahoo로 대체)
  - Yahoo Finance 차트: VIX, 감시 종목 현재가 + 200/240/365 거래일 단순이동평균
수동 입력 (manual_inputs.json)
  - MMF 총자산(ICI), 마진데트(FINRA), 내부자 매수/매도(Form 4)
"""
import csv
import io
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
# FRED는 브라우저형 UA를 봇으로 보고 응답을 지연시키고, Yahoo는 브라우저형 UA가 필요함
UA_FRED = {"User-Agent": "market-dashboard/1.0"}
UA_YAHOO = {"User-Agent": "Mozilla/5.0"}
errors = []


def http_get(url, headers, timeout=30, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # 일시 오류는 재시도
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def utc_date(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def local_date(ts, gmtoffset=0):
    """거래소 현지 날짜. 한국장 일봉은 KST 00:00(=전날 UTC 15:00)으로 찍혀서 UTC로 바꾸면 하루 밀림."""
    return datetime.fromtimestamp(ts + (gmtoffset or 0), tz=timezone.utc).date().isoformat()


# ---------------------------------------------------------------- FRED
# 수집 순서: ① FRED 공식 API (환경변수 FRED_API_KEY가 있을 때)
#           ② FRED 그래프 CSV (키 불필요, 단 GitHub Actions 같은 클라우드 IP는 차단됨)
#           ③ 대체 출처 (NY 연준 API, Yahoo 지수) — FRED에만 있는 지표는 대체 불가
FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()


def _start(days):
    return (date.today() - timedelta(days=days)).isoformat()


def fred_api_rows(series_id, days=400):
    url = ("https://api.stlouisfed.org/fred/series/observations"
           f"?series_id={series_id}&api_key={FRED_KEY}&file_type=json&observation_start={_start(days)}")
    obs = json.loads(http_get(url, UA_FRED, timeout=20, retries=1))["observations"]
    return [(o["date"], float(o["value"])) for o in obs if o["value"] not in (".", "")]


def fred_csv_rows(series_id, days=400):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={_start(days)}"
    text = http_get(url, UA_FRED, timeout=12, retries=0)
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) != 2 or row[0] in ("DATE", "observation_date"):
            continue
        try:
            rows.append((row[0], float(row[1])))
        except ValueError:
            continue  # 휴장일 등 '.' 값
    return rows


def nyfed_effr_rows(days=400):
    url = f"https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json?startDate={_start(days)}&endDate={date.today().isoformat()}"
    rates = json.loads(http_get(url, UA_FRED))["refRates"]
    return sorted((r["effectiveDate"], float(r["percentRate"])) for r in rates)


def nyfed_rrp_rows(days=400):
    url = f"https://markets.newyorkfed.org/api/rp/reverserepo/propositions/search.json?startDate={_start(days)}&endDate={date.today().isoformat()}"
    ops = json.loads(http_get(url, UA_FRED))["repo"]["operations"]
    daily = {}
    for o in ops:  # 하루 여러 번 운영되면 합산, 달러 → 십억달러
        daily[o["operationDate"]] = daily.get(o["operationDate"], 0) + o.get("totalAmtAccepted", 0) / 1e9
    return sorted(daily.items())


def yahoo_rows(symbol):
    pairs, _ = yahoo_closes(symbol, rng="2y")
    return [(utc_date(t), c) for t, c in pairs]


# FRED가 막혔을 때 쓸 대체 출처 (같은 단위로 반환)
ALT_SOURCES = {
    "DGS10": ("Yahoo ^TNX (시장 호가, 재무부 고시와 소폭 차이)", lambda: yahoo_rows("^TNX")),
    "EFFR": ("NY 연준 EFFR API", nyfed_effr_rows),
    "RRPONTSYD": ("NY 연준 역레포 운영 결과 API", nyfed_rrp_rows),
    "NASDAQCOM": ("Yahoo ^IXIC", lambda: yahoo_rows("^IXIC")),
    "SP500": ("Yahoo ^GSPC", lambda: yahoo_rows("^GSPC")),
}


def fred_series(series_id):
    """(rows, source) 반환. 출처를 차례로 시도하고 모두 실패하면 마지막 오류를 올린다."""
    attempts = []
    if FRED_KEY:
        attempts.append((f"FRED API {series_id}", lambda: fred_api_rows(series_id)))
    attempts.append((f"FRED {series_id}", lambda: fred_csv_rows(series_id)))
    if series_id in ALT_SOURCES:
        attempts.append(ALT_SOURCES[series_id])
    last = None
    for source, fn in attempts:
        try:
            rows = fn()
            if rows:
                return rows, source
            last = ValueError("데이터 없음")
        except Exception as e:
            last = e
    raise last


def fred_latest(series_id, scale=1.0, digits=2):
    try:
        rows, source = fred_series(series_id)
        d, v = rows[-1]
        out = {"value": round(v * scale, digits), "asOf": d, "source": source}
        # 약 3개월 전 값 (방향 판단용)
        cutoff = (datetime.fromisoformat(d) - timedelta(days=90)).date().isoformat()
        past = [r for r in rows if r[0] <= cutoff]
        if past:
            out["value_3m_ago"] = round(past[-1][1] * scale, digits)
        if len(rows) >= 2:
            out["prev"] = round(rows[-2][1] * scale, digits)
        return out
    except Exception as e:
        errors.append(f"FRED {series_id}: {e}")
        return None


# ---------------------------------------------------------------- Yahoo
HISTORY = {}          # 종목별 일봉 OHLCV (차트용, history.js로 저장)
HISTORY_BARS = 900    # 차트 2년 + 365일선 계산에 필요한 앞부분


def yahoo_chart(symbol, rng):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(symbol)}?range={rng}&interval=1d"
    return json.loads(http_get(url, UA_YAHOO))["chart"]["result"][0]


def yahoo_closes(symbol, rng="3y"):
    data = yahoo_chart(symbol, rng)
    ts = data.get("timestamp") or []
    closes = data["indicators"]["quote"][0]["close"]
    pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
    return pairs, data["meta"]


def yahoo_ohlcv(symbol, rng="5y"):
    data = yahoo_chart(symbol, rng)
    q = data["indicators"]["quote"][0]
    rows = [
        (t, o, h, l, c, v)
        for t, o, h, l, c, v in zip(data.get("timestamp") or [], q["open"], q["high"], q["low"], q["close"], q["volume"])
        if None not in (o, h, l, c)
    ]
    return rows, data["meta"]


def sma(values, n):
    if len(values) < n:
        return None
    return round(sum(values[-n:]) / n, 2)


def stock_snapshot(symbol, label=None):
    try:
        rows, meta = yahoo_ohlcv(symbol)
        off = meta.get("gmtoffset", 0)
        closes = [r[4] for r in rows]
        price = meta.get("regularMarketPrice") or closes[-1]
        last_ts = rows[-1][0]
        tail = rows[-HISTORY_BARS:]
        HISTORY[symbol] = {
            "t": [local_date(r[0], off) for r in tail],
            "o": [round(r[1], 2) for r in tail],
            "h": [round(r[2], 2) for r in tail],
            "l": [round(r[3], 2) for r in tail],
            "c": [round(r[4], 2) for r in tail],
            "v": [int(r[5] or 0) for r in tail],
        }
        high_252 = max(closes[-252:])
        ret_63 = round((closes[-1] / closes[-64] - 1) * 100, 2) if len(closes) > 64 else None
        return {
            "symbol": symbol,
            "label": label or symbol,
            "name": meta.get("longName") or meta.get("shortName") or symbol,
            "currency": meta.get("currency") or "USD",
            "market": "KR" if (meta.get("currency") == "KRW") else "US",
            "price": round(price, 2),
            "asOf": local_date(last_ts, off),
            "sma200": sma(closes, 200),
            "sma240": sma(closes, 240),
            "sma365": sma(closes, 365),
            "high52w": round(high_252, 2),
            "drawdown_from_high_pct": round((price / high_252 - 1) * 100, 2),
            "ret_3m_pct": ret_63,
            "bars": len(closes),
        }
    except Exception as e:
        errors.append(f"Yahoo {symbol}: {e}")
        return None


def fx_snapshot(symbol):
    """원/달러 환율: 현재값, 200일 평균, 3개월 전 값."""
    try:
        pairs, meta = yahoo_closes(symbol, rng="2y")
        closes = [c for _, c in pairs]
        off = meta.get("gmtoffset", 0)
        return {
            "value": round(meta.get("regularMarketPrice") or closes[-1], 2),
            "asOf": local_date(pairs[-1][0], off),
            "prev": round(closes[-2], 2),
            "value_3m_ago": round(closes[-64], 2) if len(closes) > 64 else None,
            "sma200": sma(closes, 200),
            "source": f"Yahoo {symbol}",
        }
    except Exception as e:
        errors.append(f"Yahoo {symbol}: {e}")
        return None


def vix_snapshot():
    try:
        pairs, meta = yahoo_closes("^VIX", rng="1y")
        closes = [c for _, c in pairs]
        recent = closes[-60:]  # 최근 약 3개월
        peak = max(recent)
        peak_idx = len(closes) - len(recent) + recent.index(peak)
        price = meta.get("regularMarketPrice") or closes[-1]
        return {
            "value": round(price, 2),
            "asOf": utc_date(pairs[-1][0]),
            "peak_60d_close": round(peak, 2),
            "peak_60d_date": utc_date(pairs[peak_idx][0]),
            "high52w_intraday": meta.get("fiftyTwoWeekHigh"),
            "source": "Yahoo ^VIX (종가 기준)",
        }
    except Exception as e:
        errors.append(f"Yahoo ^VIX: {e}")
        return None


# ---------------------------------------------------------------- main
def main():
    manual = json.loads((BASE / "manual_inputs.json").read_text(encoding="utf-8"))
    prev = {}
    prev_path = BASE / "market_data.json"
    if prev_path.exists():
        try:
            prev = json.loads(prev_path.read_text(encoding="utf-8"))
        except Exception:
            prev = {}

    fred_jobs = {
        "us10y": ("DGS10", 1.0, 2),
        "effr": ("EFFR", 1.0, 2),
        "hy_oas": ("BAMLH0A0HYM2", 1.0, 2),
        "ccc_oas": ("BAMLH0A3HYC", 1.0, 2),
        "reserves_trillion": ("WRESBAL", 1e-6, 3),  # 백만달러 → 조달러
        "rrp_billion": ("RRPONTSYD", 1.0, 3),
        "m2_billion": ("M2SL", 1.0, 1),
        "nasdaq": ("NASDAQCOM", 1.0, 2),
        "sp500": ("SP500", 1.0, 2),
    }
    rot = manual["rotation_pair"]
    kr = manual.get("watchlist_kr", {})          # {"005930.KS": "삼성전자", ...}
    fx_sym = manual.get("fx_symbol", "KRW=X")
    yahoo_syms = list(dict.fromkeys(manual["watchlist"] + list(kr) + [rot["defensive"], rot["luxury"]]))
    with ThreadPoolExecutor(max_workers=6) as pool:
        fred_f = {k: pool.submit(fred_latest, *args) for k, args in fred_jobs.items()}
        vix_f = pool.submit(vix_snapshot)
        stock_f = {s: pool.submit(stock_snapshot, s, kr.get(s)) for s in yahoo_syms}
        fx_f = pool.submit(fx_snapshot, fx_sym)
        macro = {k: f.result() for k, f in fred_f.items()}
        snaps = {s: f.result() for s, f in stock_f.items()}
        vix_now = vix_f.result()
        macro["usdkrw"] = fx_f.result()
    # 실패한 항목은 이전 실행 값으로 대체하고 표시
    for k, v in macro.items():
        if v is None and prev.get("macro", {}).get(k):
            macro[k] = dict(prev["macro"][k], stale=True)

    vix = vix_now or (dict(prev["vix"], stale=True) if prev.get("vix") else None)

    stocks = {}
    for sym in manual["watchlist"] + list(kr):
        snap = snaps.get(sym)
        if snap is None and prev.get("stocks", {}).get(sym):
            snap = dict(prev["stocks"][sym], stale=True)
        if snap:
            stocks[sym] = snap

    rotation = {
        "defensive": snaps.get(rot["defensive"]) or prev.get("rotation", {}).get("defensive"),
        "luxury": snaps.get(rot["luxury"]) or prev.get("rotation", {}).get("luxury"),
    }

    # 마진데트 / M2 비율
    margin = manual["margin_debt_trillion"]
    margin_to_m2 = None
    if macro.get("m2_billion"):
        margin_to_m2 = round(margin["value"] * 1000 / macro["m2_billion"]["value"] * 100, 2)

    out = {
        "generatedAt": datetime.now().isoformat(timespec="minutes"),
        "macro": macro,
        "vix": vix,
        "stocks": stocks,
        "rotation": rotation,
        "manual": {
            "mmf": manual["mmf_total_trillion"],
            "margin": dict(margin, to_m2_pct=margin_to_m2),
            "insider": manual["insider_90d"],
        },
        "errors": errors,
    }

    js = "// 자동 생성 파일 — 직접 고치지 말고 update_data.py를 실행하세요.\nwindow.MARKET_DATA = " + json.dumps(out, ensure_ascii=False, indent=2) + ";\n"
    (BASE / "market_data.js").write_text(js, encoding="utf-8")

    # 차트용 일봉 — 실패한 종목은 이전 파일의 값을 유지
    hist_path = BASE / "history.json"
    hist = {}
    if hist_path.exists():
        try:
            hist = json.loads(hist_path.read_text(encoding="utf-8"))
        except Exception:
            hist = {}
    hist.update(HISTORY)
    hist = {k: v for k, v in hist.items() if k in stocks}
    compact = json.dumps(hist, ensure_ascii=False, separators=(",", ":"))
    hist_path.write_text(compact, encoding="utf-8")
    (BASE / "history.js").write_text(
        "// 자동 생성 파일 — 차트용 일봉 OHLCV\nwindow.MARKET_HISTORY = " + compact + ";\n", encoding="utf-8")
    prev_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"갱신 완료: {out['generatedAt']}  종목 {len(stocks)}개")
    if errors:
        print("실패 항목:")
        for e in errors:
            print("  -", e)
        sys.exit(1 if not stocks else 0)


if __name__ == "__main__":
    main()
