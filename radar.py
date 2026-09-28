import os
import time
import requests
from statistics import median
from concurrent.futures import ThreadPoolExecutor, as_completed

# =========================================================
# CRYPTO AV RADARI v6
# CHR + CELR + ONE + QI + HBAR
# HIZLI ANOMALI + BIRIKIM + TREND + FUTURES/OI
# =========================================================

SPOT = "https://data-api.binance.vision"
FUTURES = "https://fapi.binance.com"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ----- HIZLI MOTOR -----
EARLY_VOLUME_MULTIPLE = 3.0
STRONG_VOLUME_MULTIPLE = 5.0
MIN_TAKER_RATIO = 60.0
STRONG_TAKER_RATIO = 68.0
MIN_QUOTE_VOLUME = 100_000
STRONG_QUOTE_VOLUME = 300_000
MIN_PRICE_MOVE_5M = 0.05
MAX_PRICE_MOVE_5M = 10.0
MAX_PRICE_MOVE_15M = 15.0
MIN_ELAPSED_SECONDS = 45
MAX_BREAKOUT_DISTANCE = 1.5

# ----- BIRIKIM -----
ACCUM_VOLUME_MULTIPLE = 2.0
ACCUM_MIN_TAKER = 56.0
ACCUM_MIN_QUOTE = 200_000
PERSISTENT_TAKER_MIN = 55.0

# ----- TREND -----
TREND_MIN_1H = -2.0
TREND_MAX_1H = 6.0
TREND_MIN_4H = 1.5
TREND_MAX_4H = 10.0
TREND_MIN_24H = 2.0
TREND_MAX_24H = 18.0
TREND_VOLUME_MULTIPLE = 1.5

# ----- FUTURES -----
MIN_FUTURES_MULTIPLE = 3.0
MIN_OI_CHANGE = 2.0

# ----- PATTERN MOTORLARI -----
CHR_VOL = 4.0
CHR_OI = 2.0

CELR_VOL = 8.0
CELR_PRICE_MIN = 3.0
CELR_FUTURES = 5.0
CELR_OI = 2.0

ONE_VOL = 2.5
ONE_PRICE_MIN = 2.0
ONE_TAKER = 60.0

QI_FIRST_VOL = 3.0
QI_SECOND_VOL = 5.0
QI_TAKER = 55.0

# HBAR: spot + futures patlamasi. OI/taker teyidi gecikebilir.
HBAR_SPOT_VOL = 5.0
HBAR_FUTURES_VOL = 5.0
HBAR_PRICE_MIN = 3.0
HBAR_PRICE_MAX = 10.0

LATE_24H = 25.0

MAX_DEEP_SCAN = 40
MAX_ALERTS = 5

EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "USDE", "USDS",
    "EUR", "TRY", "BRL", "GBP", "JPY", "AUD", "BIDR"
}

session = requests.Session()
session.headers.update({"User-Agent": "Crypto-Av-Radari/6.0"})


def get_json(url, params=None, timeout=6):
    r = session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram secret eksik.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message},
            timeout=8,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print("Telegram hatasi:", e)
        return False


def pct(a, b):
    if not a:
        return 0.0
    return ((b / a) - 1) * 100


def safe_median(values):
    cleaned = []
    for value in values:
        try:
            value = float(value)
            if value > 0:
                cleaned.append(value)
        except Exception:
            pass
    return median(cleaned) if cleaned else 0.0


def fmt(value, suffix="", digits=2):
    return "-" if value is None else f"{value:+.{digits}f}{suffix}"


def money(value):
    if value is None:
        return "-"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"


def candle_taker_ratio(candle):
    q = float(candle[7])
    tq = float(candle[10])
    return (tq / q * 100) if q > 0 else 0.0


def spot_symbols():
    info = get_json(f"{SPOT}/api/v3/exchangeInfo")
    result = []
    for s in info["symbols"]:
        base = s["baseAsset"]
        # Binance Crypto sekmesindeki gerÃ§ek USDT spotlarÄ± hedefle.
        if (
            s["quoteAsset"] == "USDT"
            and s["status"] == "TRADING"
            and s.get("isSpotTradingAllowed", True)
            and base not in EXCLUDED_BASES
        ):
            result.append(s["symbol"])
    return result


def futures_symbols():
    try:
        info = get_json(f"{FUTURES}/fapi/v1/exchangeInfo", timeout=5)
        return {
            s["symbol"]
            for s in info["symbols"]
            if s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
            and s.get("contractType") == "PERPETUAL"
        }
    except Exception as e:
        print("Futures API kullanilamiyor:", e)
        return set()


def klines(symbol, interval="5m", limit=20):
    return get_json(
        f"{SPOT}/api/v3/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )


def futures_klines(symbol, limit=14):
    return get_json(
        f"{FUTURES}/fapi/v1/klines",
        {"symbol": symbol, "interval": "5m", "limit": limit},
        timeout=5,
    )


def oi_history(symbol, limit=8):
    return get_json(
        f"{FUTURES}/futures/data/openInterestHist",
        {"symbol": symbol, "period": "5m", "limit": limit},
        timeout=5,
    )


def funding_rate(symbol):
    try:
        x = get_json(
            f"{FUTURES}/fapi/v1/premiumIndex",
            {"symbol": symbol},
            timeout=5,
        )
        return float(x.get("lastFundingRate", 0)) * 100
    except Exception:
        return None


def ticker_24h(symbol):
    try:
        x = get_json(f"{SPOT}/api/v3/ticker/24hr", {"symbol": symbol})
        return float(x["priceChangePercent"])
    except Exception:
        return None


def oi_changes(oi):
    """5/15/30 dk OI degisimleri. API 5dk snapshot verir."""
    if not oi:
        return None, None, None
    vals = [float(x["sumOpenInterest"]) for x in oi]
    newest = vals[-1]

    def change_back(steps):
        if len(vals) <= steps:
            return None
        return pct(vals[-1 - steps], newest)

    return change_back(1), change_back(3), change_back(6)


def pre_scan(symbol):
    try:
        candles = klines(symbol, "5m", 14)
        if len(candles) < 12:
            return None

        current = candles[-1]
        previous = candles[-9:-1]
        baseline = safe_median(c[7] for c in previous)
        if baseline <= 0:
            return None

        elapsed = (int(time.time() * 1000) - int(current[0])) / 1000
        elapsed = min(max(elapsed, 1), 300)
        if elapsed < MIN_ELAPSED_SECONDS:
            return None

        progress = elapsed / 300
        q = float(current[7])
        actual = q / baseline
        projected = actual / progress
        move = pct(float(current[1]), float(current[4]))
        taker = candle_taker_ratio(current)

        # HBAR icin taker zorunlu degil: hacim patlamasi aday havuzuna girmeli.
        volume_only_candidate = (
            q >= MIN_QUOTE_VOLUME
            and projected >= HBAR_SPOT_VOL
            and -1.0 <= move <= HBAR_PRICE_MAX
        )

        normal_candidate = (
            q >= MIN_QUOTE_VOLUME
            and projected >= EARLY_VOLUME_MULTIPLE
            and taker >= ACCUM_MIN_TAKER
            and -1.5 <= move <= MAX_PRICE_MOVE_5M
        )

        if not (volume_only_candidate or normal_candidate):
            return None

        if elapsed < 90 and actual < 0.40:
            return None

        return {
            "symbol": symbol,
            "projected_quote_multiple": projected,
            "quote_volume": q,
        }
    except Exception:
        return None


def deep_scan(candidate, futures_set):
    symbol = candidate["symbol"]
    try:
        candles = klines(symbol, "5m", 40)
        if len(candles) < 30:
            return None

        current = candles[-1]
        completed = candles[:-1]
        baseline = safe_median(c[7] for c in completed[-8:])
        if baseline <= 0:
            return None

        elapsed = (int(time.time() * 1000) - int(current[0])) / 1000
        elapsed = min(max(elapsed, MIN_ELAPSED_SECONDS), 300)
        progress = elapsed / 300

        price = float(current[4])
        q = float(current[7])
        actual_vol = q / baseline
        projected_vol = actual_vol / progress
        move_5m = pct(float(current[1]), price)
        taker = candle_taker_ratio(current)

        one = klines(symbol, "1m", 8)
        move_1m = pct(float(one[-1][1]), float(one[-1][4])) if one else None

        fifteen = klines(symbol, "15m", 12)
        move_15m = (
            pct(float(fifteen[-1][1]), float(fifteen[-1][4]))
            if fifteen else None
        )

        # Onceki tamamlanmis 5dk mumlar: QI hacim merdiveni.
        prev1 = completed[-1]
        prev2 = completed[-2]
        prev1_mult = float(prev1[7]) / baseline
        prev2_mult = float(prev2[7]) / baseline
        volume_staircase = (
            (prev2_mult >= QI_FIRST_VOL and prev1_mult >= QI_SECOND_VOL)
            or (prev1_mult >= QI_FIRST_VOL and projected_vol >= QI_SECOND_VOL)
        )

        taker_hist = [candle_taker_ratio(c) for c in completed[-3:]]
        persistent_taker = (
            len(taker_hist) == 3
            and safe_median(taker_hist) >= PERSISTENT_TAKER_MIN
        )

        recent_highs = [float(c[2]) for c in completed[-12:]]
        breakout = max(recent_highs) if recent_highs else None
        breakout_distance = pct(price, breakout) if breakout else None
        breakout_near = (
            breakout_distance is not None
            and 0 < breakout_distance <= MAX_BREAKOUT_DISTANCE
        )
        breakout_done = (
            breakout_distance is not None
            and -2.0 <= breakout_distance <= 0
        )

        futures_multiple = None
        oi5 = oi15 = oi30 = None
        funding = None

        if symbol in futures_set:
            try:
                fc = futures_klines(symbol)
                if len(fc) >= 10:
                    fcur = fc[-1]
                    fbase = safe_median(c[7] for c in fc[-9:-1])
                    if fbase > 0:
                        futures_multiple = float(fcur[7]) / fbase / progress

                oi = oi_history(symbol)
                oi5, oi15, oi30 = oi_changes(oi)
                funding = funding_rate(symbol)
            except Exception as e:
                print(symbol, "futures/OI yok:", e)

        change24 = ticker_24h(symbol)

        # ---------------- PATTERN TANIMA ----------------
        patterns = []

        chr_type = (
            1.0 <= move_5m <= 8.0
            and projected_vol >= CHR_VOL
            and max([x for x in (oi15, oi30) if x is not None] or [-999]) >= CHR_OI
        )
        if chr_type:
            patterns.append("CHR")

        celr_type = (
            CELR_PRICE_MIN <= move_5m <= 8.0
            and projected_vol >= CELR_VOL
            and futures_multiple is not None
            and futures_multiple >= CELR_FUTURES
            and max([x for x in (oi15, oi30) if x is not None] or [-999]) >= CELR_OI
        )
        if celr_type:
            patterns.append("CELR")

        one_type = (
            ONE_PRICE_MIN <= move_5m <= 5.0
            and projected_vol >= ONE_VOL
            and taker >= ONE_TAKER
        )
        if one_type:
            patterns.append("ONE")

        qi_type = (
            1.0 <= move_5m <= 12.0
            and volume_staircase
            and taker >= QI_TAKER
        )
        if qi_type:
            patterns.append("QI")

        # HBAR: ilk mumda OI/taker gecikebilir.
        hbar_type = (
            HBAR_PRICE_MIN <= move_5m <= HBAR_PRICE_MAX
            and projected_vol >= HBAR_SPOT_VOL
            and futures_multiple is not None
            and futures_multiple >= HBAR_FUTURES_VOL
        )
        if hbar_type:
            patterns.append("HBAR")

        # Erken fakat tam kaliba oturmayan kaliteli aday.
        early_watch = (
            0.0 <= move_5m <= 10.0
            and projected_vol >= EARLY_VOLUME_MULTIPLE
            and (
                taker >= ACCUM_MIN_TAKER
                or (futures_multiple is not None and futures_multiple >= MIN_FUTURES_MULTIPLE)
                or max([x for x in (oi15, oi30) if x is not None] or [-999]) >= MIN_OI_CHANGE
            )
        )

        if not patterns and not early_watch:
            return None

        late = (
            (change24 is not None and change24 >= LATE_24H)
            or (move_15m is not None and move_15m >= 18.0)
        )

        # ---------------- PUAN ----------------
        score = 0
        if projected_vol >= 3: score += 2
        if projected_vol >= 5: score += 1
        if projected_vol >= 8: score += 1
        if taker >= 60: score += 1
        if persistent_taker: score += 1
        if futures_multiple is not None and futures_multiple >= 3: score += 2
        if oi15 is not None and oi15 >= 2: score += 2
        if oi30 is not None and oi30 >= 5: score += 1
        if breakout_near or breakout_done: score += 1
        if volume_staircase: score += 1

        if late:
            level = "ð´ GEC"
        elif celr_type:
            level = "ð¨ GUCLU ERKEN ANOMALI"
        elif patterns:
            level = "ð¨ ERKEN ANOMALI"
        else:
            level = "ð¡ ERKEN AV"

        return {
            "symbol": symbol,
            "price": price,
            "move_1m": move_1m,
            "move_5m": move_5m,
            "move_15m": move_15m,
            "move_24h": change24,
            "quote_volume": q,
            "projected_quote_multiple": projected_vol,
            "taker_ratio": taker,
            "futures_multiple": futures_multiple,
            "oi5": oi5,
            "oi15": oi15,
            "oi30": oi30,
            "funding": funding,
            "breakout": breakout,
            "patterns": patterns or ["GENEL"],
            "volume_staircase": volume_staircase,
            "level": level,
            "score": score,
            "type": "FAST",
        }

    except Exception as e:
        print(symbol, "derin analiz hatasi:", e)
        return None


def trend_pre_scan(symbol):
    try:
        candles = klines(symbol, "1h", 26)
        if len(candles) < 25:
            return None
        completed = candles[:-1]
        price = float(candles[-1][4])
        move_1h = pct(float(completed[-1][1]), price)
        move_4h = pct(float(completed[-4][1]), price)
        move_24h = pct(float(completed[-24][1]), price)

        recent_vol = safe_median(c[7] for c in completed[-4:])
        old_vol = safe_median(c[7] for c in completed[-16:-4])
        if old_vol <= 0:
            return None
        regime = recent_vol / old_vol

        lows = [float(c[3]) for c in completed[-4:]]
        closes = [float(c[4]) for c in completed[-4:]]
        higher_lows = lows[-1] >= lows[-2] and lows[-2] >= lows[-3]
        positive_closes = sum(
            closes[i] > closes[i - 1] for i in range(1, len(closes))
        )

        if (
            TREND_MIN_1H <= move_1h <= TREND_MAX_1H
            and TREND_MIN_4H <= move_4h <= TREND_MAX_4H
            and TREND_MIN_24H <= move_24h <= TREND_MAX_24H
            and regime >= TREND_VOLUME_MULTIPLE
            and (higher_lows or positive_closes >= 2)
        ):
            return {"symbol": symbol, "volume_regime": regime}
    except Exception:
        pass
    return None


def trend_deep_scan(candidate):
    symbol = candidate["symbol"]
    try:
        candles = klines(symbol, "1h", 50)
        if len(candles) < 30:
            return None

        completed = candles[:-1]
        price = float(candles[-1][4])
        move_1h = pct(float(completed[-1][1]), price)
        move_4h = pct(float(completed[-4][1]), price)
        move_24h = pct(float(completed[-24][1]), price)

        if not (
            TREND_MIN_1H <= move_1h <= TREND_MAX_1H
            and TREND_MIN_4H <= move_4h <= TREND_MAX_4H
            and TREND_MIN_24H <= move_24h <= TREND_MAX_24H
        ):
            return None

        recent_vol = safe_median(c[7] for c in completed[-4:])
        old_vol = safe_median(c[7] for c in completed[-20:-4])
        if old_vol <= 0:
            return None
        regime = recent_vol / old_vol

        recent = completed[-5:]
        lows = [float(c[3]) for c in recent]
        highs = [float(c[2]) for c in recent]
        closes = [float(c[4]) for c in recent]

        hl = sum(lows[i] >= lows[i - 1] for i in range(1, len(lows)))
        hh = sum(highs[i] >= highs[i - 1] for i in range(1, len(highs)))
        pc = sum(closes[i] > closes[i - 1] for i in range(1, len(closes)))

        score = 0
        if regime >= 1.5: score += 2
        if regime >= 2.5: score += 1
        if hl >= 2: score += 1
        if hh >= 2: score += 1
        if pc >= 2: score += 1
        if move_4h >= TREND_MIN_4H: score += 1
        if move_24h >= TREND_MIN_24H: score += 1

        if score < 6:
            return None

        level = "ðµ ERKEN TREND"
        if score >= 8 and hl >= 3 and hh >= 3 and move_1h <= 4 and move_4h <= 8:
            level = "ð GUCLU TREND DEVAMI"

        return {
            "symbol": symbol,
            "price": price,
            "move_1h": move_1h,
            "move_4h": move_4h,
            "move_24h": move_24h,
            "volume_regime": regime,
            "higher_lows": hl,
            "higher_highs": hh,
            "level": level,
            "score": score,
            "type": "TREND",
        }
    except Exception as e:
        print(symbol, "trend analiz hatasi:", e)
        return None


def format_fast_alert(x):
    futures_text = (
        f"{x['futures_multiple']:.1f}x"
        if x["futures_multiple"] is not None else "-"
    )
    funding_text = fmt(x["funding"], "%", 4)
    staircase = "EVET" if x["volume_staircase"] else "HAYIR"
    pattern = "/".join(x["patterns"])

    return (
        f"{x['level']}\n\n"
        f"ðª {x['symbol']}\n"
        f"ð§¬ Kalip: {pattern}\n"
        f"ðµ Fiyat: {x['price']}\n\n"
        f"â¡ 1dk: {fmt(x['move_1m'], '%')}\n"
        f"ð 5dk: {fmt(x['move_5m'], '%')}\n"
        f"ð 15dk: {fmt(x['move_15m'], '%')}\n"
        f"ð 24s: {fmt(x['move_24h'], '%')}\n\n"
        f"ð° Acik 5dk hacim: {money(x['quote_volume'])}\n"
        f"ð¥ 5dk hacim hizi: {x['projected_quote_multiple']:.1f}x\n"
        f"ðª QI hacim merdiveni: {staircase}\n"
        f"ð¢ Spot taker: %{x['taker_ratio']:.1f}\n\n"
        f"âï¸ Futures hacim: {futures_text}\n"
        f"ð OI 5dk: {fmt(x['oi5'], '%')}\n"
        f"ð OI 15dk: {fmt(x['oi15'], '%')}\n"
        f"ð OI 30dk: {fmt(x['oi30'], '%')}\n"
        f"ð¸ Funding: {funding_text}\n\n"
        f"ð¯ Son 12 mum tepe: {x['breakout'] or '-'}\n"
        f"â­ Puan: {x['score']}\n\n"
        f"â ï¸ Giris bolgesi adayidir; otomatik alim emri degildir."
    )


def format_trend_alert(x):
    return (
        f"{x['level']}\n\n"
        f"ðª {x['symbol']}\n"
        f"ðµ Fiyat: {x['price']}\n\n"
        f"â± 1s: {fmt(x['move_1h'], '%')}\n"
        f"ð 4s: {fmt(x['move_4h'], '%')}\n"
        f"ð 24s: {fmt(x['move_24h'], '%')}\n\n"
        f"ð¥ Hacim rejimi: {x['volume_regime']:.1f}x\n"
        f"âï¸ Yukselen dip: {x['higher_lows']}/4\n"
        f"ð Yukselen tepe: {x['higher_highs']}/4\n"
        f"â­ Trend puani: {x['score']}\n\n"
        f"â ï¸ Radar adayidir; otomatik alim emri degildir."
    )


def main():
    start = time.time()
    print("Crypto Av Radari v6 basladi.")

    symbols = spot_symbols()
    print("Taranacak USDT spot:", len(symbols))

    futures_set = futures_symbols()
    print("Futures sembol:", len(futures_set))

    # MOTOR 1
    fast_candidates = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        jobs = {executor.submit(pre_scan, s): s for s in symbols}
        for future in as_completed(jobs):
            try:
                r = future.result()
                if r:
                    fast_candidates.append(r)
            except Exception:
                pass

    fast_candidates.sort(
        key=lambda x: (x["projected_quote_multiple"], x["quote_volume"]),
        reverse=True,
    )
    fast_candidates = fast_candidates[:MAX_DEEP_SCAN]
    print("Hizli/birikim adayi:", len(fast_candidates))

    alerts = []
    for c in fast_candidates:
        r = deep_scan(c, futures_set)
        if r:
            alerts.append(r)

    # MOTOR 2
    trend_candidates = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        jobs = {executor.submit(trend_pre_scan, s): s for s in symbols}
        for future in as_completed(jobs):
            try:
                r = future.result()
                if r:
                    trend_candidates.append(r)
            except Exception:
                pass

    trend_candidates.sort(
        key=lambda x: x["volume_regime"],
        reverse=True,
    )
    trend_candidates = trend_candidates[:MAX_DEEP_SCAN]
    print("Trend adayi:", len(trend_candidates))

    for c in trend_candidates:
        r = trend_deep_scan(c)
        if r:
            alerts.append(r)

    # Ayni calismada ayni coin icin en yuksek puanli alarmi tut.
    best = {}
    for a in alerts:
        s = a["symbol"]
        if s not in best or a["score"] > best[s]["score"]:
            best[s] = a

    unique = sorted(best.values(), key=lambda x: x["score"], reverse=True)

    print("Toplam tarama suresi:", f"{time.time() - start:.1f}s")

    if not unique:
        print("Yeni anlamli sinyal yok.")
        return

    print("Kaliteli alarm:", len(unique))

    for a in unique[:MAX_ALERTS]:
        message = (
            format_trend_alert(a)
            if a["type"] == "TREND"
            else format_fast_alert(a)
        )
        telegram(message)
        print(a["symbol"], a["level"], "score:", a["score"])


if __name__ == "__main__":
    main()
