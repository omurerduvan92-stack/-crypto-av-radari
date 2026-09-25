import os
import time
import requests
from statistics import median
from concurrent.futures import ThreadPoolExecutor, as_completed

# =========================================================
# CRYPTO AV RADARI v4
# =========================================================

SPOT = "https://data-api.binance.vision"
FUTURES = "https://fapi.binance.com"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# =========================================================
# AYARLAR
# =========================================================

# Hacim anomalisi
EARLY_VOLUME_MULTIPLE = 3.0
STRONG_VOLUME_MULTIPLE = 5.0

# Spot alış baskısı
MIN_TAKER_RATIO = 60.0
STRONG_TAKER_RATIO = 68.0

# Açık 5dk mumda minimum gerçek USDT işlem hacmi
# Düşük likiditeli sahte 20x/50x sinyalleri azaltır.
MIN_QUOTE_VOLUME = 100_000

# Daha güçlü alarm için minimum USDT hacmi
STRONG_QUOTE_VOLUME = 300_000

# Fiyat hareketi
MIN_PRICE_MOVE_5M = 0.05
MAX_PRICE_MOVE_5M = 7.0

# 15dk'da zaten çok uçmuşsa kovalamıyoruz
MAX_PRICE_MOVE_15M = 12.0

# Açık mumun ilk saniyelerinde tarama yapma
MIN_ELAPSED_SECONDS = 45

# Breakout'a yakınlık
MAX_BREAKOUT_DISTANCE = 1.5

# Futures
MIN_FUTURES_MULTIPLE = 3.0
MIN_OI_CHANGE = 2.0

# Performans
MAX_DEEP_SCAN = 30
MAX_ALERTS = 3

# Aynı GitHub run içinde duplicate engeli
sent_this_run = set()

EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI",
    "EUR", "TRY", "BRL", "GBP", "JPY", "AUD",
    "BIDR"
}

session = requests.Session()

session.headers.update({
    "User-Agent": "Crypto-Av-Radari/4.0"
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

        print(
            "Futures API kullanilamiyor:",
            e
        )

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
# ÖN TARAMA
# =========================================================

def pre_scan(symbol):

    try:

        candles = klines(
            symbol,
            "5m",
            12
        )

        if len(candles) < 10:
            return None

        current = candles[-1]
        previous = candles[-9:-1]

        current_open = float(current[1])
        current_close = float(current[4])

        # Base asset hacmi
        current_volume = float(current[5])

        # USDT cinsinden gerçek işlem hacmi
        current_quote_volume = float(current[7])

        # Taker BUY quote volume
        taker_buy_quote = float(current[10])

        baseline_volume = safe_median([
            c[5]
            for c in previous
        ])

        baseline_quote = safe_median([
            c[7]
            for c in previous
        ])

        if (
            baseline_volume <= 0
            or baseline_quote <= 0
        ):
            return None

        open_time = int(current[0])

        elapsed = (
            int(time.time() * 1000)
            - open_time
        ) / 1000

        elapsed = min(
            max(elapsed, 1),
            300
        )

        if elapsed < MIN_ELAPSED_SECONDS:
            return None

        progress = elapsed / 300

        # Açık mumun bugüne kadarki hacmi
        current_multiple = (
            current_volume /
            baseline_volume
        )

        quote_multiple = (
            current_quote_volume /
            baseline_quote
        )

        # 5dk sonuna giderse yaklaşık ne olur?
        projected_multiple = (
            current_multiple /
            progress
        )

        projected_quote_multiple = (
            quote_multiple /
            progress
        )

        move_5m = pct(
            current_open,
            current_close
        )

        taker_ratio = (
            taker_buy_quote /
            current_quote_volume * 100
            if current_quote_volume > 0
            else 0
        )

        # ---------------------------------------------
        # TEMEL FİLTRELER
        # ---------------------------------------------

        # Gerçek dolar hacmi küçükse çöpe at
        if current_quote_volume < MIN_QUOTE_VOLUME:
            return None

        # Hacim ivmesi yok
        if projected_quote_multiple < EARLY_VOLUME_MULTIPLE:
            return None

        # Alıcı baskısı yeterli değil
        if taker_ratio < MIN_TAKER_RATIO:
            return None

        # Fiyat henüz yukarı uyanmamış
        if move_5m < MIN_PRICE_MOVE_5M:
            return None

        # Çoktan uçmuş
        if move_5m > MAX_PRICE_MOVE_5M:
            return None

        # İlk bölümde projeksiyon aşırı şişmesin
        if (
            elapsed < 90
            and quote_multiple < 0.45
        ):
            return None

        return {
            "symbol": symbol,
            "current_multiple":
                current_multiple,
            "projected_multiple":
                projected_multiple,
            "quote_multiple":
                quote_multiple,
            "projected_quote_multiple":
                projected_quote_multiple,
            "quote_volume":
                current_quote_volume,
            "move_5m":
                move_5m,
            "taker_ratio":
                taker_ratio
        }

    except Exception:
        return None


# =========================================================
# DERİN ANALİZ
# =========================================================

def deep_scan(candidate, futures_set):

    symbol = candidate["symbol"]

    try:

        candles = klines(
            symbol,
            "5m",
            20
        )

        if len(candles) < 14:
            return None

        current = candles[-1]
        previous = candles[-9:-1]

        current_open = float(current[1])
        current_close = float(current[4])

        current_volume = float(current[5])
        current_quote_volume = float(current[7])

        taker_buy_quote = float(current[10])

        baseline_volume = safe_median([
            c[5]
            for c in previous
        ])

        baseline_quote = safe_median([
            c[7]
            for c in previous
        ])

        if (
            baseline_volume <= 0
            or baseline_quote <= 0
        ):
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

        current_multiple = (
            current_volume /
            baseline_volume
        )

        quote_multiple = (
            current_quote_volume /
            baseline_quote
        )

        projected_multiple = (
            current_multiple /
            progress
        )

        projected_quote_multiple = (
            quote_multiple /
            progress
        )

        move_5m = pct(
            current_open,
            current_close
        )

        taker_ratio = (
            taker_buy_quote /
            current_quote_volume * 100
            if current_quote_volume > 0
            else 0
        )

        # =================================================
        # 1 DK
        # =================================================

        one = klines(
            symbol,
            "1m",
            6
        )

        move_1m = None
        one_minute_quote = None

        if one:

            m = one[-1]

            move_1m = pct(
                float(m[1]),
                float(m[4])
            )

            one_minute_quote = float(m[7])

        # =================================================
        # 15 DK
        # =================================================

        fifteen = klines(
            symbol,
            "15m",
            3
        )

        move_15m = None

        if fifteen:

            m = fifteen[-1]

            move_15m = pct(
                float(m[1]),
                float(m[4])
            )

        if (
            move_15m is not None
            and move_15m >
            MAX_PRICE_MOVE_15M
        ):
            return None

        # =================================================
        # BREAKOUT
        # =================================================

        recent_highs = [
            float(c[2])
            for c in candles[-13:-1]
        ]

        breakout = (
            max(recent_highs)
            if recent_highs
            else None
        )

        breakout_distance = None

        if breakout:

            # Pozitif = breakout seviyesi fiyatın üzerinde.
            # Negatif = fiyat breakout'u geçmiş.
            breakout_distance = pct(
                current_close,
                breakout
            )

        breakout_near = (
            breakout_distance is not None
            and
            -3.0 <= breakout_distance <=
            MAX_BREAKOUT_DISTANCE
        )

        breakout_done = (
            breakout_distance is not None
            and breakout_distance <= 0
        )

        # =================================================
        # FUTURES + OI
        # =================================================

        futures_multiple = None
        oi_change = None

        if symbol in futures_set:

            try:

                fc = futures_klines(symbol)

                if len(fc) >= 10:

                    f_current = fc[-1]
                    f_previous = fc[-9:-1]

                    f_volume = float(
                        f_current[5]
                    )

                    f_baseline = safe_median([
                        c[5]
                        for c in f_previous
                    ])

                    if f_baseline > 0:

                        futures_multiple = (
                            f_volume /
                            progress /
                            f_baseline
                        )

                oi = oi_history(symbol)

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
        # BAĞIMSIZ TEYİTLER
        # =================================================

        volume_confirmed = (
            projected_quote_multiple >=
            EARLY_VOLUME_MULTIPLE
        )

        strong_volume = (
            projected_quote_multiple >=
            STRONG_VOLUME_MULTIPLE
            and current_quote_volume >=
            STRONG_QUOTE_VOLUME
        )

        taker_confirmed = (
            taker_ratio >=
            MIN_TAKER_RATIO
        )

        strong_taker = (
            taker_ratio >=
            STRONG_TAKER_RATIO
        )

        momentum_confirmed = (
            move_5m >=
            MIN_PRICE_MOVE_5M
            and move_5m <=
            MAX_PRICE_MOVE_5M
            and (
                move_1m is None
                or move_1m > -0.25
            )
        )

        futures_confirmed = (
            futures_multiple is not None
            and futures_multiple >=
            MIN_FUTURES_MULTIPLE
        )

        oi_confirmed = (
            oi_change is not None
            and oi_change >=
            MIN_OI_CHANGE
        )

        # =================================================
        # ZORUNLU SPOT ŞARTLARI
        # =================================================

        if not volume_confirmed:
            return None

        if not taker_confirmed:
            return None

        if not momentum_confirmed:
            return None

        if current_quote_volume < MIN_QUOTE_VOLUME:
            return None

        # =================================================
        # PUANLAMA
        # =================================================

        score = 0

        # Gerçek USDT hacim ivmesi
        if volume_confirmed:
            score += 1

        if strong_volume:
            score += 1

        # Alıcı baskısı
        if strong_taker:
            score += 1

        # Fiyat davranışı
        if momentum_confirmed:
            score += 1

        # Breakout yapısı
        if breakout_near:
            score += 1

        if breakout_done:
            score += 1

        # Bağımsız vadeli teyitler
        if futures_confirmed:
            score += 2

        if oi_confirmed:
            score += 2

        # =================================================
        # ALARM SEVİYESİ
        # =================================================

        # Telegram'a çıkmak için artık daha sert şart.
        if score < 4:
            return None

        level = "🟠 ERKEN AV"

        # Futures yokken sadece spot verisiyle
        # kolayca "çok güçlü" demiyoruz.
        if (
            score >= 6
            and (
                futures_confirmed
                or oi_confirmed
            )
        ):

            level = (
                "🚨 GUCLU ERKEN ANOMALI"
            )

        elif (
            score >= 5
            and strong_volume
            and strong_taker
        ):

            level = (
                "🟠 GUCLU SPOT ANOMALISI"
            )

        return {
            "symbol":
                symbol,

            "price":
                current_close,

            "move_1m":
                move_1m,

            "move_5m":
                move_5m,

            "move_15m":
                move_15m,

            "current_multiple":
                current_multiple,

            "projected_multiple":
                projected_multiple,

            "quote_multiple":
                quote_multiple,

            "projected_quote_multiple":
                projected_quote_multiple,

            "quote_volume":
                current_quote_volume,

            "one_minute_quote":
                one_minute_quote,

            "taker_ratio":
                taker_ratio,

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
                score
        }

    except Exception as e:

        print(
            symbol,
            "derin analiz hatasi:",
            e
        )

        return None


# =========================================================
# ALERT
# =========================================================

def format_alert(x):

    futures_text = (
        f"{x['futures_multiple']:.1f}x"
        if x["futures_multiple"]
        is not None
        else "-"
    )

    oi_text = (
        fmt(
            x["oi_change"],
            "%"
        )
        if x["oi_change"]
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

    return (
        f"{x['level']}\n\n"

        f"🪙 {x['symbol']}\n"

        f"💵 Fiyat: "
        f"{x['price']}\n"

        f"⚡ 1dk: "
        f"{fmt(x['move_1m'], '%')}\n"

        f"📈 5dk: "
        f"{fmt(x['move_5m'], '%')}\n"

        f"📊 15dk: "
        f"{fmt(x['move_15m'], '%')}\n\n"

        f"💰 Bu 5dk mumunda: "
        f"{money(x['quote_volume'])}\n"

        f"🔥 Gerçek hacim: "
        f"{x['quote_multiple']:.1f}x\n"

        f"🚀 Hacim hız tahmini: "
        f"{x['projected_quote_multiple']:.1f}x\n"

        f"🟢 Spot taker alış: "
        f"%{x['taker_ratio']:.1f}\n\n"

        f"⚙️ Futures hacim: "
        f"{futures_text}\n"

        f"📊 OI ~10dk: "
        f"{oi_text}\n\n"

        f"🎯 Breakout: "
        f"{breakout_text}\n"

        f"📍 Breakout mesafe: "
        f"{breakout_distance}\n"

        f"⭐ Radar puanı: "
        f"{x['score']}\n\n"

        f"⚠️ Erken anomali takibidir; "
        f"otomatik alim sinyali degildir."
    )


# =========================================================
# MAIN
# =========================================================

def main():

    start = time.time()

    print(
        "Crypto Av Radari v4 basladi."
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
    # ÖN TARAMA
    # =====================================================

    candidates = []

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
                    candidates.append(
                        result
                    )

            except Exception:
                pass

    # Sadece "x" oranına değil,
    # gerçek USDT hacmine de ağırlık ver.
    candidates.sort(
        key=lambda x: (
            x[
                "projected_quote_multiple"
            ],
            x["quote_volume"]
        ),
        reverse=True
    )

    candidates = candidates[
        :MAX_DEEP_SCAN
    ]

    print(
        "On tarama adayi:",
        len(candidates)
    )

    # =====================================================
    # DERİN TARAMA
    # =====================================================

    alerts = []

    for candidate in candidates:

        result = deep_scan(
            candidate,
            futures_set
        )

        if result:

            if (
                result["symbol"]
                not in sent_this_run
            ):

                sent_this_run.add(
                    result["symbol"]
                )

                alerts.append(
                    result
                )

    # Puan + gerçek para hacmi + ivme
    alerts.sort(
        key=lambda x: (
            x["score"],
            x["quote_volume"],
            x[
                "projected_quote_multiple"
            ]
        ),
        reverse=True
    )

    elapsed = time.time() - start

    print(
        "Toplam tarama suresi:",
        f"{elapsed:.1f}s"
    )

    if not alerts:

        print(
            "Yeni anlamli erken "
            "sinyal yok."
        )

        return

    print(
        "Kaliteli alarm:",
        len(alerts)
    )

    # =====================================================
    # TELEGRAM
    # =====================================================

    for alert in alerts[
        :MAX_ALERTS
    ]:

        message = format_alert(
            alert
        )

        telegram(message)

        print(
            alert["symbol"],
            alert["level"],
            "USDT hacim:",
            money(
                alert["quote_volume"]
            ),
            "hacim:",
            f"{alert['projected_quote_multiple']:.1f}x",
            "taker:",
            f"%{alert['taker_ratio']:.1f}",
            "score:",
            alert["score"]
        )


if __name__ == "__main__":
    main()
