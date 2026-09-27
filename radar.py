import os
import time
import requests
from statistics import median
from concurrent.futures import ThreadPoolExecutor, as_completed

# =========================================================
# CRYPTO AV RADARI v5
# HIZLI ANOMALI + BIRIKIM + TREND
# =========================================================

SPOT = "https://data-api.binance.vision"
FUTURES = "https://fapi.binance.com"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# =========================================================
# AYARLAR
# =========================================================

# ----- HIZLI MOTOR -----
EARLY_VOLUME_MULTIPLE = 3.0
STRONG_VOLUME_MULTIPLE = 5.0

MIN_TAKER_RATIO = 60.0
STRONG_TAKER_RATIO = 68.0

MIN_QUOTE_VOLUME = 100_000
STRONG_QUOTE_VOLUME = 300_000

MIN_PRICE_MOVE_5M = 0.05
MAX_PRICE_MOVE_5M = 7.0
MAX_PRICE_MOVE_15M = 12.0

MIN_ELAPSED_SECONDS = 45
MAX_BREAKOUT_DISTANCE = 1.5

# ----- BIRIKIM MOTORU -----
ACCUM_VOLUME_MULTIPLE = 2.0
ACCUM_STRONG_VOLUME = 3.0
ACCUM_MIN_TAKER = 56.0
ACCUM_MAX_MOVE_30M = 6.0
ACCUM_MIN_QUOTE = 200_000

# Son 3 adet tamamlanmis 5dk mumda
# alis baskisi devam ediyor mu?
PERSISTENT_TAKER_MIN = 55.0

# ----- TREND MOTORU v5.1 -----

# ZEC / ONT / LSK tipi erken yuruyen trendleri ara.
# Tek saatte coktan firlamis veya sert dump yiyenleri ele.

TREND_MIN_1H = -2.0
TREND_MAX_1H = 6.0

TREND_MIN_4H = 1.5
TREND_MAX_4H = 10.0

TREND_MIN_24H = 2.0
TREND_MAX_24H = 18.0

# Son saatlerde hacim eski rejimin en az 1.5 kati olmali.
TREND_VOLUME_MULTIPLE = 1.5

# ----- FUTURES -----
MIN_FUTURES_MULTIPLE = 3.0
MIN_OI_CHANGE = 2.0

# ----- PERFORMANS -----
MAX_DEEP_SCAN = 40
MAX_ALERTS = 5

sent_this_run = set()

EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI",
    "EUR", "TRY", "BRL", "GBP", "JPY", "AUD",
    "BIDR"
}

session = requests.Session()

session.headers.update({
    "User-Agent": "Crypto-Av-Radari/5.0"
})


# =========================================================
# HTTP
# =========================================================

def get_json(url, params=None, timeout=6):

    r = session.get(
        url,
        params=params,
        timeout=timeout
    )

    r.raise_for_status()

    return r.json()


# =========================================================
# TELEGRAM
# =========================================================

def telegram(message):

    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram secret eksik.")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    try:

        r = requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=8
        )

        r.raise_for_status()
        return True

    except Exception as e:

        print("Telegram hatasi:", e)
        return False


# =========================================================
# YARDIMCI
# =========================================================

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

    if not cleaned:
        return 0.0

    return median(cleaned)


def fmt(value, suffix="", digits=2):

    if value is None:
        return "-"

    return f"{value:+.{digits}f}{suffix}"


def money(value):

    if value is None:
        return "-"

    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"

    if value >= 1_000:
        return f"${value / 1_000:.0f}K"

    return f"${value:.0f}"


def candle_taker_ratio(candle):

    quote_volume = float(candle[7])
    taker_quote = float(candle[10])

    if quote_volume <= 0:
        return 0.0

    return taker_quote / quote_volume * 100


# =========================================================
# SPOT SEMBOLLER
# =========================================================

def spot_symbols():

    info = get_json(
        f"{SPOT}/api/v3/exchangeInfo"
    )

    result = []

    for s in info["symbols"]:

        if (
            s["quoteAsset"] == "USDT"
            and s["status"] == "TRADING"
            and s.get("isSpotTradingAllowed", True)
            and s["baseAsset"] not in EXCLUDED_BASES
        ):

            result.append(s["symbol"])

    return result


# =========================================================
# FUTURES
# =========================================================

def futures_symbols():

    try:

        info = get_json(
            f"{FUTURES}/fapi/v1/exchangeInfo",
            timeout=5
        )

        return {
            s["symbol"]
            for s in info["symbols"]
            if (
                s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"
                and s.get("contractType") == "PERPETUAL"
            )
        }

    except Exception as e:

        print("Futures API kullanilamiyor:", e)
        print("Spot radar devam ediyor.")

        return set()


# =========================================================
# KLINE
# =========================================================

def klines(symbol, interval="5m", limit=20):

    return get_json(
        f"{SPOT}/api/v3/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
    )


def futures_klines(symbol):

    return get_json(
        f"{FUTURES}/fapi/v1/klines",
        {
            "symbol": symbol,
            "interval": "5m",
            "limit": 12
        },
        timeout=5
    )


def oi_history(symbol):

    return get_json(
        f"{FUTURES}/futures/data/openInterestHist",
        {
            "symbol": symbol,
            "period": "5m",
            "limit": 4
        },
        timeout=5
    )


# =========================================================
# 5DK HIZLI ON TARAMA
# =========================================================

def pre_scan(symbol):

    try:

        candles = klines(symbol, "5m", 14)

        if len(candles) < 12:
            return None

        current = candles[-1]
        previous = candles[-9:-1]

        current_open = float(current[1])
        current_close = float(current[4])
        current_quote = float(current[7])

        baseline_quote = safe_median([
            c[7] for c in previous
        ])

        if baseline_quote <= 0:
            return None

        open_time = int(current[0])

        elapsed = (
            int(time.time() * 1000)
            - open_time
        ) / 1000

        elapsed = min(max(elapsed, 1), 300)

        if elapsed < MIN_ELAPSED_SECONDS:
            return None

        progress = elapsed / 300

        quote_multiple = (
            current_quote /
            baseline_quote
        )

        projected_quote_multiple = (
            quote_multiple /
            progress
        )

        move_5m = pct(
            current_open,
            current_close
        )

        taker_ratio = candle_taker_ratio(
            current
        )

        # HIZLI ANOMALI ADAYI
        fast_candidate = (
            current_quote >= MIN_QUOTE_VOLUME
            and projected_quote_multiple >=
            EARLY_VOLUME_MULTIPLE
            and taker_ratio >= MIN_TAKER_RATIO
            and -1.0 <= move_5m <=
            MAX_PRICE_MOVE_5M
        )

        # BIRIKIM ADAYI:
        # Fiyat daha uyanmadan da iceri alabilir.
        accumulation_candidate = (
            current_quote >=
            MIN_QUOTE_VOLUME
            and projected_quote_multiple >=
            ACCUM_VOLUME_MULTIPLE
            and taker_ratio >=
            ACCUM_MIN_TAKER
            and -1.5 <= move_5m <= 5.0
        )

        if not (
            fast_candidate
            or accumulation_candidate
        ):
            return None

        # İlk saniyelerde projeksiyon sismesin
        if (
            elapsed < 90
            and quote_multiple < 0.40
        ):
            return None

        return {
            "symbol": symbol,
            "quote_volume": current_quote,
            "quote_multiple": quote_multiple,
            "projected_quote_multiple":
                projected_quote_multiple,
            "move_5m": move_5m,
            "taker_ratio": taker_ratio,
            "fast_candidate":
                fast_candidate,
            "accumulation_candidate":
                accumulation_candidate
        }

    except Exception:
        return None


# =========================================================
# TREND ON TARAMA
# =========================================================

def trend_pre_scan(symbol):

    try:

        candles = klines(
            symbol,
            "1h",
            26
        )

        if len(candles) < 25:
            return None

        completed = candles[:-1]

        current_price = float(
            candles[-1][4]
        )

        price_1h = float(
            completed[-1][1]
        )

        price_4h = float(
            completed[-4][1]
        )

        price_24h = float(
            completed[-24][1]
        )

        move_1h = pct(
            price_1h,
            current_price
        )

        move_4h = pct(
            price_4h,
            current_price
        )

        move_24h = pct(
            price_24h,
            current_price
        )

        recent_volume = safe_median([
            c[7]
            for c in completed[-4:]
        ])

        old_volume = safe_median([
            c[7]
            for c in completed[-16:-4]
        ])

        if old_volume <= 0:
            return None

        volume_regime = (
            recent_volume /
            old_volume
        )

        # Son 4 saat dipleri
        lows = [
            float(c[3])
            for c in completed[-4:]
        ]

        # Son 4 saat kapanislari
        closes = [
            float(c[4])
            for c in completed[-4:]
        ]

        higher_lows = (
            lows[-1] >= lows[-2]
            and lows[-2] >= lows[-3]
        )

        positive_closes = sum(
            1
            for i in range(1, len(closes))
            if closes[i] > closes[i - 1]
        )

        trend_candidate = (
            TREND_MIN_4H <= move_4h <=
            TREND_MAX_4H
            and TREND_MIN_24H <= move_24h <=
            TREND_MAX_24H
            and volume_regime >=
            TREND_VOLUME_MULTIPLE
            and (
                higher_lows
                or positive_closes >= 2
            )
        )

        if not trend_candidate:
            return None

        return {
            "symbol": symbol,
            "move_1h": move_1h,
            "move_4h": move_4h,
            "move_24h": move_24h,
            "volume_regime":
                volume_regime
        }

    except Exception:
        return None


# =========================================================
# DERIN ANALIZ
# =========================================================

def deep_scan(candidate, futures_set):

    symbol = candidate["symbol"]

    try:

        candles = klines(
            symbol,
            "5m",
            40
        )

        if len(candles) < 30:
            return None

        current = candles[-1]
        completed = candles[:-1]

        current_open = float(current[1])
        current_close = float(current[4])
        current_quote = float(current[7])

        baseline_quote = safe_median([
            c[7]
            for c in completed[-8:]
        ])

        if baseline_quote <= 0:
            return None

        open_time = int(current[0])

        elapsed = (
            int(time.time() * 1000)
            - open_time
        ) / 1000

        elapsed = min(
            max(elapsed, MIN_ELAPSED_SECONDS),
            300
        )

        progress = elapsed / 300

        quote_multiple = (
            current_quote /
            baseline_quote
        )

        projected_quote_multiple = (
            quote_multiple /
            progress
        )

        move_5m = pct(
            current_open,
            current_close
        )

        taker_ratio = candle_taker_ratio(
            current
        )

        # =================================================
        # 1DK
        # =================================================

        one = klines(
            symbol,
            "1m",
            8
        )

        move_1m = None

        if one:

            move_1m = pct(
                float(one[-1][1]),
                float(one[-1][4])
            )

        # =================================================
        # 15DK + 30DK
        # =================================================

        fifteen = klines(
            symbol,
            "15m",
            20
        )

        move_15m = None
        volume_15m_multiple = None

        if len(fifteen) >= 10:

            f_current = fifteen[-1]
            f_previous = fifteen[-9:-1]

            move_15m = pct(
                float(f_current[1]),
                float(f_current[4])
            )

            base_15 = safe_median([
                c[7]
                for c in f_previous
            ])

            if base_15 > 0:

                elapsed_15 = (
                    int(time.time() * 1000)
                    - int(f_current[0])
                ) / 1000

                elapsed_15 = min(
                    max(elapsed_15, 1),
                    900
                )

                volume_15m_multiple = (
                    float(f_current[7])
                    / base_15
                    / (elapsed_15 / 900)
                )

        thirty = klines(
            symbol,
            "30m",
            14
        )

        move_30m = None
        volume_30m_multiple = None

        if len(thirty) >= 10:

            t_current = thirty[-1]
            t_previous = thirty[-9:-1]

            move_30m = pct(
                float(t_current[1]),
                float(t_current[4])
            )

            base_30 = safe_median([
                c[7]
                for c in t_previous
            ])

            if base_30 > 0:

                elapsed_30 = (
                    int(time.time() * 1000)
                    - int(t_current[0])
                ) / 1000

                elapsed_30 = min(
                    max(elapsed_30, 1),
                    1800
                )

                volume_30m_multiple = (
                    float(t_current[7])
                    / base_30
                    / (elapsed_30 / 1800)
                )

        # =================================================
        # TAKER SUREKLILIGI
        # =================================================

        last_three = completed[-3:]

        taker_history = [
            candle_taker_ratio(c)
            for c in last_three
        ]

        persistent_taker = (
            len(taker_history) == 3
            and safe_median(
                taker_history
            ) >= PERSISTENT_TAKER_MIN
        )

        # =================================================
        # BREAKOUT
        # =================================================

        recent_highs = [
            float(c[2])
            for c in completed[-12:]
        ]

        breakout = (
            max(recent_highs)
            if recent_highs
            else None
        )

        breakout_distance = None

        if breakout:

            breakout_distance = pct(
                current_close,
                breakout
            )

        breakout_near = (
            breakout_distance is not None
            and 0 <
            breakout_distance <=
            MAX_BREAKOUT_DISTANCE
        )

        breakout_done = (
            breakout_distance is not None
            and -2.0 <=
            breakout_distance <= 0
        )

        # =================================================
        # FUTURES + OI
        # =================================================

        futures_multiple = None
        oi_change = None

        if symbol in futures_set:

            try:

                fc = futures_klines(
                    symbol
                )

                if len(fc) >= 10:

                    f_current = fc[-1]
                    f_previous = fc[-9:-1]

                    f_baseline = safe_median([
                        c[7]
                        for c in f_previous
                    ])

                    if f_baseline > 0:

                        futures_multiple = (
                            float(f_current[7])
                            / f_baseline
                            / progress
                        )

                oi = oi_history(
                    symbol
                )

                if len(oi) >= 3:

                    old_oi = float(
                        oi[-3][
                            "sumOpenInterest"
                        ]
                    )

                    new_oi = float(
                        oi[-1][
                            "sumOpenInterest"
                        ]
                    )

                    oi_change = pct(
                        old_oi,
                        new_oi
                    )

            except Exception as e:

                print(
                    symbol,
                    "futures/OI yok:",
                    e
                )

        # =================================================
        # PUANLAMA
        # =================================================

        score = 0

        if (
            projected_quote_multiple >=
            EARLY_VOLUME_MULTIPLE
        ):
            score += 2

        if (
            projected_quote_multiple >=
            STRONG_VOLUME_MULTIPLE
            and current_quote >=
            STRONG_QUOTE_VOLUME
        ):
            score += 1

        if taker_ratio >= MIN_TAKER_RATIO:
            score += 1

        if taker_ratio >= STRONG_TAKER_RATIO:
            score += 1

        if persistent_taker:
            score += 1

        if (
            volume_15m_multiple is not None
            and volume_15m_multiple >= 2
        ):
            score += 1

        if (
            volume_30m_multiple is not None
            and volume_30m_multiple >=
            ACCUM_VOLUME_MULTIPLE
        ):
            score += 2

        if breakout_near:
            score += 1

        # breakout ayni yapinin ikinci puani
        # olmasin; yakin VE kirilmis ayni anda
        # puanlanmiyor.
        elif breakout_done:
            score += 1

        if (
            futures_multiple is not None
            and futures_multiple >=
            MIN_FUTURES_MULTIPLE
        ):
            score += 2

        if (
            oi_change is not None
            and oi_change >=
            MIN_OI_CHANGE
        ):
            score += 2

        # =================================================
        # SINIFLANDIRMA
        # =================================================

        accumulation = (
            current_quote >=
            ACCUM_MIN_QUOTE
            and -1.5 <= move_5m <= 5.0
            and taker_ratio >=
            ACCUM_MIN_TAKER
            and (
                (
                    volume_15m_multiple
                    is not None
                    and
                    volume_15m_multiple >=
                    ACCUM_VOLUME_MULTIPLE
                )
                or
                (
                    volume_30m_multiple
                    is not None
                    and
                    volume_30m_multiple >=
                    ACCUM_VOLUME_MULTIPLE
                )
            )
        )

        fast_signal = (
            projected_quote_multiple >=
            EARLY_VOLUME_MULTIPLE
            and taker_ratio >=
            MIN_TAKER_RATIO
            and -1.0 <=
            move_5m <=
            MAX_PRICE_MOVE_5M
        )

        if not (
            accumulation
            or fast_signal
        ):
            return None

        if score < 4:
            return None

        level = "🟠 ERKEN AV"

        if (
            accumulation
            and not fast_signal
        ):
            level = (
                "🟣 BIRIKIM TESPIT EDILDI"
            )

        if (
            score >= 7
            and (
                futures_multiple is not None
                or oi_change is not None
            )
        ):
            level = (
                "🚨 GUCLU ERKEN ANOMALI"
            )

        elif (
            score >= 6
            and projected_quote_multiple >=
            STRONG_VOLUME_MULTIPLE
            and taker_ratio >=
            STRONG_TAKER_RATIO
        ):
            level = (
                "🟠 GUCLU SPOT ANOMALISI"
            )

        return {
            "symbol": symbol,
            "price": current_close,
            "move_1m": move_1m,
            "move_5m": move_5m,
            "move_15m": move_15m,
            "move_30m": move_30m,
            "quote_volume":
                current_quote,
            "quote_multiple":
                quote_multiple,
            "projected_quote_multiple":
                projected_quote_multiple,
            "volume_15m_multiple":
                volume_15m_multiple,
            "volume_30m_multiple":
                volume_30m_multiple,
            "taker_ratio":
                taker_ratio,
            "persistent_taker":
                persistent_taker,
            "futures_multiple":
                futures_multiple,
            "oi_change":
                oi_change,
            "breakout":
                breakout,
            "breakout_distance":
                breakout_distance,
            "level":
                level,
            "score":
                score,
            "type":
                "FAST"
        }

    except Exception as e:

        print(
            symbol,
            "derin analiz hatasi:",
            e
        )

        return None


# =========================================================
# TREND DERIN ANALIZ
# =========================================================

def trend_deep_scan(candidate):

    symbol = candidate["symbol"]

    try:

        candles = klines(
            symbol,
            "1h",
            50
        )

        if len(candles) < 30:
            return None

        completed = candles[:-1]
        current = candles[-1]

        price = float(
            current[4]
        )

        move_1h = pct(
            float(completed[-1][1]),
            price
        )

        move_4h = pct(
            float(completed[-4][1]),
            price
        )

        move_24h = pct(
            float(completed[-24][1]),
            price
        )
# =================================================
# v5.1 ERKEN TREND FILTRESI
# =================================================

# Son 1 saatte sert dump yiyen coin trend adayi degil.
if move_1h < TREND_MIN_1H:
    return None

# Tek saatte coktan firlamis coini kovalamiyoruz.
if move_1h > TREND_MAX_1H:
    return None

# 4 saatlik hareket daha yeni baslamis olmali.
if (
    move_4h < TREND_MIN_4H
    or move_4h > TREND_MAX_4H
):
    return None

# 24 saatte coktan parabolik hale gelenleri ele.
if (
    move_24h < TREND_MIN_24H
    or move_24h > TREND_MAX_24H
):
    return None
        recent_vol = safe_median([
            c[7]
            for c in completed[-4:]
        ])

        old_vol = safe_median([
            c[7]
            for c in completed[-20:-4]
        ])

        if old_vol <= 0:
            return None

        volume_regime = (
            recent_vol /
            old_vol
        )

        # 4 saatlik yapi
        recent = completed[-5:]

        lows = [
            float(c[3])
            for c in recent
        ]

        highs = [
            float(c[2])
            for c in recent
        ]

        closes = [
            float(c[4])
            for c in recent
        ]

        higher_low_count = sum(
            1
            for i in range(1, len(lows))
            if lows[i] >= lows[i - 1]
        )

        higher_high_count = sum(
            1
            for i in range(1, len(highs))
            if highs[i] >= highs[i - 1]
        )

        positive_close_count = sum(
            1
            for i in range(1, len(closes))
            if closes[i] >
            closes[i - 1]
        )

        score = 0

        if (
            TREND_MIN_4H <=
            move_4h <=
            TREND_MAX_4H
        ):
            score += 2

        if (
            TREND_MIN_24H <=
            move_24h <=
            TREND_MAX_24H
        ):
            score += 1

        if volume_regime >= 1.5:
            score += 2

        if volume_regime >= 2.5:
            score += 1

        if higher_low_count >= 2:
            score += 1

        if higher_high_count >= 2:
            score += 1

        if positive_close_count >= 2:
            score += 1

        # Çoktan aşırı parabolik hale gelmişse
        # yeni trend başlangıcı diye göndermiyoruz.
        if (
            move_4h > TREND_MAX_4H
            or move_24h >
            TREND_MAX_24H
        ):
            return None

        if score < 6:
            return None

       # =================================================
# v5.1 TREND SINIFLANDIRMA
# =================================================

level = "🔵 ERKEN TREND"

# Trend oturmaya baslamis ama fiyat hala
# erken kabul ettigimiz bolgede olmali.
if (
    score >= 8
    and higher_low_count >= 3
    and higher_high_count >= 3
    and move_1h <= 4.0
    and move_4h <= 8.0
):
    level = "📈 GUCLU TREND DEVAMI"

        return {
            "symbol": symbol,
            "price": price,
            "move_1h": move_1h,
            "move_4h": move_4h,
            "move_24h": move_24h,
            "volume_regime":
                volume_regime,
            "higher_lows":
                higher_low_count,
            "higher_highs":
                higher_high_count,
            "level": level,
            "score": score,
            "type": "TREND"
        }

    except Exception as e:

        print(
            symbol,
            "trend analiz hatasi:",
            e
        )

        return None


# =========================================================
# ALERT
# =========================================================

def format_fast_alert(x):

    futures_text = (
        f"{x['futures_multiple']:.1f}x"
        if x["futures_multiple"]
        is not None
        else "-"
    )

    oi_text = (
        fmt(x["oi_change"], "%")
        if x["oi_change"]
        is not None
        else "-"
    )

    v15 = (
        f"{x['volume_15m_multiple']:.1f}x"
        if x["volume_15m_multiple"]
        is not None
        else "-"
    )

    v30 = (
        f"{x['volume_30m_multiple']:.1f}x"
        if x["volume_30m_multiple"]
        is not None
        else "-"
    )

    breakout_text = (
        str(x["breakout"])
        if x["breakout"]
        is not None
        else "-"
    )

    breakout_distance = (
        fmt(
            x["breakout_distance"],
            "%"
        )
        if x["breakout_distance"]
        is not None
        else "-"
    )

    taker_persistence = (
        "EVET"
        if x["persistent_taker"]
        else "HAYIR"
    )

    return (
        f"{x['level']}\n\n"

        f"🪙 {x['symbol']}\n"
        f"💵 Fiyat: {x['price']}\n\n"

        f"⚡ 1dk: "
        f"{fmt(x['move_1m'], '%')}\n"

        f"📈 5dk: "
        f"{fmt(x['move_5m'], '%')}\n"

        f"📊 15dk: "
        f"{fmt(x['move_15m'], '%')}\n"

        f"📊 30dk: "
        f"{fmt(x['move_30m'], '%')}\n\n"

        f"💰 Acik 5dk hacim: "
        f"{money(x['quote_volume'])}\n"

        f"🔥 Gercek 5dk hacim: "
        f"{x['quote_multiple']:.1f}x\n"

        f"🚀 5dk hacim hizi: "
        f"{x['projected_quote_multiple']:.1f}x\n"

        f"📊 15dk hacim hizi: "
        f"{v15}\n"

        f"📊 30dk hacim hizi: "
        f"{v30}\n\n"

        f"🟢 Spot taker: "
        f"%{x['taker_ratio']:.1f}\n"

        f"🔁 Taker surekliligi: "
        f"{taker_persistence}\n\n"

        f"⚙️ Futures: "
        f"{futures_text}\n"

        f"📊 OI ~10dk: "
        f"{oi_text}\n\n"

        f"🎯 Breakout: "
        f"{breakout_text}\n"

        f"📍 Mesafe: "
        f"{breakout_distance}\n"

        f"⭐ Puan: "
        f"{x['score']}\n\n"

        f"⚠️ Radar adayidir; "
        f"otomatik alim sinyali degildir."
    )


def format_trend_alert(x):

    return (
        f"{x['level']}\n\n"

        f"🪙 {x['symbol']}\n"
        f"💵 Fiyat: {x['price']}\n\n"

        f"⏱ 1s: "
        f"{fmt(x['move_1h'], '%')}\n"

        f"📈 4s: "
        f"{fmt(x['move_4h'], '%')}\n"

        f"📊 24s: "
        f"{fmt(x['move_24h'], '%')}\n\n"

        f"🔥 Hacim rejimi: "
        f"{x['volume_regime']:.1f}x\n"

        f"↗️ Yukselen dip: "
        f"{x['higher_lows']}/4\n"

        f"🚀 Yukselen tepe: "
        f"{x['higher_highs']}/4\n\n"

        f"⭐ Trend puani: "
        f"{x['score']}\n\n"

        f"📌 ZEC / ONT / LSK tipi "
        f"orta vade trend adayi.\n"

        f"⚠️ Radar adayidir; "
        f"otomatik alim sinyali degildir."
    )


# =========================================================
# MAIN
# =========================================================

def main():

    start = time.time()

    print(
        "Crypto Av Radari v5 basladi."
    )

    symbols = spot_symbols()

    print(
        "Taranacak USDT spot:",
        len(symbols)
    )

    futures_set = futures_symbols()

    print(
        "Futures sembol:",
        len(futures_set)
    )

    # =====================================================
    # MOTOR 1: HIZLI + BIRIKIM
    # =====================================================

    fast_candidates = []

    with ThreadPoolExecutor(
        max_workers=12
    ) as executor:

        jobs = {
            executor.submit(
                pre_scan,
                symbol
            ): symbol
            for symbol in symbols
        }

        for future in as_completed(
            jobs
        ):

            try:

                result = future.result()

                if result:
                    fast_candidates.append(
                        result
                    )

            except Exception:
                pass

    fast_candidates.sort(
        key=lambda x: (
            x[
                "projected_quote_multiple"
            ],
            x["quote_volume"]
        ),
        reverse=True
    )

    fast_candidates = fast_candidates[
        :MAX_DEEP_SCAN
    ]

    print(
        "Hizli/birikim adayi:",
        len(fast_candidates)
    )

    alerts = []

    for candidate in fast_candidates:

        result = deep_scan(
            candidate,
            futures_set
        )

        if result:

            alerts.append(result)

    # =====================================================
    # MOTOR 2: TREND
    # =====================================================

    trend_candidates = []

    with ThreadPoolExecutor(
        max_workers=12
    ) as executor:

        jobs = {
            executor.submit(
                trend_pre_scan,
                symbol
            ): symbol
            for symbol in symbols
        }

        for future in as_completed(
            jobs
        ):

            try:

                result = future.result()

                if result:
                    trend_candidates.append(
                        result
                    )

            except Exception:
                pass

    trend_candidates.sort(
        key=lambda x: (
            x["volume_regime"],
            x["move_4h"]
        ),
        reverse=True
    )

    trend_candidates = trend_candidates[
        :MAX_DEEP_SCAN
    ]

    print(
        "Trend adayi:",
        len(trend_candidates)
    )

    for candidate in trend_candidates:

        result = trend_deep_scan(
            candidate
        )

        if result:

            alerts.append(result)

    # =====================================================
    # DUPLICATE + SIRALAMA
    # =====================================================

    unique_alerts = []

    for alert in alerts:

        symbol = alert["symbol"]

        if symbol in sent_this_run:
            continue

        sent_this_run.add(symbol)

        unique_alerts.append(alert)

    unique_alerts.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    elapsed = time.time() - start

    print(
        "Toplam tarama suresi:",
        f"{elapsed:.1f}s"
    )

    if not unique_alerts:

        print(
            "Yeni anlamli sinyal yok."
        )
        return

    print(
        "Kaliteli alarm:",
        len(unique_alerts)
    )

    # =====================================================
    # TELEGRAM
    # =====================================================

    for alert in unique_alerts[
        :MAX_ALERTS
    ]:

        if alert["type"] == "TREND":

            message = format_trend_alert(
                alert
            )

        else:

            message = format_fast_alert(
                alert
            )

        telegram(message)

        print(
            alert["symbol"],
            alert["level"],
            "score:",
            alert["score"]
        )


if __name__ == "__main__":
    main()
