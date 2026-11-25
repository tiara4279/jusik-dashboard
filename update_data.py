"""
시장 바닥 관제 대시보드 데이터 갱신 스크립트

실행:  python update_data.py
결과:  market_data.js  (index.html이 읽는 데이터)
       market_data.json (같은 내용, 다음 실행 때 실패한 항목의 대체값으로 사용)

자동 수집
  - FRED (API 키 불필요, CSV): 10년물, EFFR, HY/CCC 스프레드, 은행 준비금, 역레포, M2, 나스닥
  - Yahoo Finance 차트: VIX, 감시 종목 현재가 + 200/240/365 거래일 단순이동평균
수동 입력 (manual_inputs.json)
  - MMF 총자산(ICI), 마진데트(FINRA), 내부자 매수/매도(Form 4)
"""
import csv
import io
import json
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


# ---------------------------------------------------------------- FRED
def fred_series(series_id, days=400):
    start = (date.today() - timedelta(days=days)).isoformat()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}"
    text = http_get(url, UA_FRED)
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) != 2 or row[0] in ("DATE", "observation_date"):
            continue
        try:
            rows.append((row[0], float(row[1])))
        except ValueError:
            continue  # 휴장일 등 '.' 값
    if not rows:
        raise ValueError(f"{series_id}: 데이터 없음")
    return rows


def fred_latest(series_id, scale=1.0, digits=2):
    try:
        rows = fred_series(series_id)
        d, v = rows[-1]
        out = {"value": round(v * scale, digits), "asOf": d, "source": f"FRED {series_id}"}
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
def yahoo_closes(symbol, rng="3y"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(symbol)}?range={rng}&interval=1d"
    data = json.loads(http_get(url, UA_YAHOO))["chart"]["result"][0]
    ts = data.get("timestamp") or []
    closes = data["indicators"]["quote"][0]["close"]
    pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
    meta = data["meta"]
    return pairs, meta


def sma(values, n):
    if len(values) < n:
        return None
    return round(sum(values[-n:]) / n, 2)


def stock_snapshot(symbol):
    try:
        pairs, meta = yahoo_closes(symbol)
        closes = [c for _, c in pairs]
        price = meta.get("regularMarketPrice") or closes[-1]
        last_ts = pairs[-1][0]
        high_252 = max(closes[-252:])
        ret_63 = round((closes[-1] / closes[-64] - 1) * 100, 2) if len(closes) > 64 else None
        return {
            "symbol": symbol,
            "name": meta.get("longName") or meta.get("shortName") or symbol,
            "price": round(price, 2),
            "asOf": utc_date(last_ts),
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
    yahoo_syms = list(dict.fromkeys(manual["watchlist"] + [rot["defensive"], rot["luxury"]]))
    with ThreadPoolExecutor(max_workers=6) as pool:
        fred_f = {k: pool.submit(fred_latest, *args) for k, args in fred_jobs.items()}
        vix_f = pool.submit(vix_snapshot)
        stock_f = {s: pool.submit(stock_snapshot, s) for s in yahoo_syms}
        macro = {k: f.result() for k, f in fred_f.items()}
        snaps = {s: f.result() for s, f in stock_f.items()}
        vix_now = vix_f.result()
    # 실패한 항목은 이전 실행 값으로 대체하고 표시
    for k, v in macro.items():
        if v is None and prev.get("macro", {}).get(k):
            macro[k] = dict(prev["macro"][k], stale=True)

    vix = vix_now or (dict(prev["vix"], stale=True) if prev.get("vix") else None)

    stocks = {}
    for sym in manual["watchlist"]:
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
    prev_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"갱신 완료: {out['generatedAt']}  종목 {len(stocks)}개")
    if errors:
        print("실패 항목:")
        for e in errors:
            print("  -", e)
        sys.exit(1 if not stocks else 0)


if __name__ == "__main__":
    main()
