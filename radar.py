import os
import time
import requests
from statistics import median
from concurrent.futures import ThreadPoolExecutor, as_completed

# =========================================================
# CRYPTO AV RADARI v3
# =========================================================

SPOT = "https://data-api.binance.vision"
FUTURES = "https://fapi.binance.com"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# -------------------------
# RADAR AYARLARI
# -------------------------

# Erken hacim
EARLY_VOLUME_MULTIPLE = 3.0

# Güçlü hacim
STRONG_VOLUME_MULTIPLE = 5.0

# Taker alış baskısı
MIN_TAKER_RATIO = 55.0

# OI artışı
MIN_OI_CHANGE = 2.0

# Futures hacim teyidi
MIN_FUTURES_MULTIPLE = 3.0

# Çoktan uçmuş coinleri kovalamama
MAX_PRICE_MOVE_5M = 10.0

# Sert negatif mumlarda pump alarmı verme
MIN_PRICE_MOVE_5M = -2.0

# Açık mumun ilk saniyelerinde projeksiyon şişmesini önle
MIN_ELAPSED_SECONDS = 45

# Ön taramadan derin analize girecek maksimum coin
MAX_DEEP_SCAN = 35

# Telegram'a gönderilecek maksimum alarm
MAX_ALERTS = 5

EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI",
    "EUR", "TRY", "BRL", "GBP", "JPY", "AUD",
    "BIDR"
}

session = requests.Session()

session.headers.update({
    "User-Agent": "Crypto-Av-Radari/3.0"
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

        return

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    try:

        requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=8
        ).raise_for_status()

    except Exception as e:

        print(
            "Telegram hatasi:",
            e
        )


# =========================================================
# YARDIMCI
# =========================================================

def pct(a, b):

    if not a:
        return 0.0

    return ((b / a) - 1) * 100


def safe_median(values):

    values = [
        float(x)
        for x in values
        if float(x) > 0
    ]

    if not values:
        return 0.0

    return median(values)


def fmt(value, suffix="", digits=2):

    if value is None:
        return "-"

    return (
        f"{value:+.{digits}f}{suffix}"
    )


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
            and s.get(
                "isSpotTradingAllowed",
                True
            )
            and s["baseAsset"]
            not in EXCLUDED_BASES
        ):

            result.append(
                s["symbol"]
            )

    return result


# =========================================================
# FUTURES SEMBOLLER
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
                and s.get("contractType")
                == "PERPETUAL"
            )
        }

    except Exception as e:

        print(
            "Futures API kullanilamiyor:",
            e
        )

        print(
            "Spot radar devam ediyor."
        )

        return set()


# =========================================================
# KLINE
# =========================================================

def klines(
    symbol,
    interval="5m",
    limit=20
):

    return get_json(
        f"{SPOT}/api/v3/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
    )


# =========================================================
# FUTURES
# =========================================================

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
# 1. AŞAMA
# HIZLI ÖN TARAMA
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

        current_volume = float(
            current[5]
        )

        current_open = float(
            current[1]
        )

        current_close = float(
            current[4]
        )

        baseline = safe_median([
            c[5]
            for c in previous
        ])

        if baseline <= 0:
            return None

        open_time = int(
            current[0]
        )

        elapsed = (
            int(time.time() * 1000)
            - open_time
        ) / 1000

        elapsed = min(
            max(elapsed, 1),
            300
        )

        # İlk 45 saniyede
        # projeksiyona güvenme
        if elapsed < MIN_ELAPSED_SECONDS:
            return None

        progress = (
            elapsed / 300
        )

        projected_volume = (
            current_volume /
            progress
        )

        projected_multiple = (
            projected_volume /
            baseline
        )

        current_multiple = (
            current_volume /
            baseline
        )

        move_5m = pct(
            current_open,
            current_close
        )

        taker_buy = float(
            current[9]
        )

        taker_ratio = (
            taker_buy /
            current_volume * 100
            if current_volume > 0
            else 0
        )

        # Hacim yeterli değil
        if (
            projected_multiple <
            EARLY_VOLUME_MULTIPLE
        ):
            return None

        # Çoktan uçmuş
        if (
            move_5m >
            MAX_PRICE_MOVE_5M
        ):
            return None

        # Sert satış
        if (
            move_5m <
            MIN_PRICE_MOVE_5M
        ):
            return None

        # Çok erken sahte spike filtresi:
        # projeksiyon yüksek olsa bile
        # gerçek mum hacmi tamamen boş olmasın.
        if (
            current_multiple < 0.35
            and elapsed < 90
        ):
            return None

        return {
            "symbol": symbol,
            "projected_multiple":
                projected_multiple,
            "current_multiple":
                current_multiple,
            "move_5m":
                move_5m,
            "taker_ratio":
                taker_ratio
        }

    except Exception:
        return None


# =========================================================
# 2. AŞAMA
# DERİN ANALİZ
# =========================================================

def deep_scan(
    candidate,
    futures_set
):

    symbol = candidate["symbol"]

    try:

        candles = klines(
            symbol,
            "5m",
            20
        )

        current = candles[-1]
        previous = candles[-9:-1]

        current_open = float(
            current[1]
        )

        current_close = float(
            current[4]
        )

        current_volume = float(
            current[5]
        )

        taker_buy_volume = float(
            current[9]
        )

        baseline = safe_median([
            c[5]
            for c in previous
        ])

        if baseline <= 0:
            return None

        open_time = int(
            current[0]
        )

        elapsed = (
            int(time.time() * 1000)
            - open_time
        ) / 1000

        elapsed = min(
            max(elapsed, MIN_ELAPSED_SECONDS),
            300
        )

        progress = (
            elapsed / 300
        )

        current_multiple = (
            current_volume /
            baseline
        )

        projected_multiple = (
            current_volume /
            progress /
            baseline
        )

        move_5m = pct(
            current_open,
            current_close
        )

        taker_ratio = (
            taker_buy_volume /
            current_volume * 100
            if current_volume > 0
            else 0
        )

        # -------------------------
        # 1 DK
        # -------------------------

        one = klines(
            symbol,
            "1m",
            3
        )

        move_1m = None

        if one:

            m = one[-1]

            move_1m = pct(
                float(m[1]),
                float(m[4])
            )

        # -------------------------
        # 15 DK
        # -------------------------

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

        # -------------------------
        # BREAKOUT
        # -------------------------

        # Açık mum hariç son mumların
        # en yüksek seviyesi
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

            breakout_distance = pct(
                current_close,
                breakout
            )

        # -------------------------
        # FUTURES + OI
        # -------------------------

        futures_multiple = None
        oi_change = None

        if symbol in futures_set:

            try:

                fc = futures_klines(
                    symbol
                )

                if len(fc) >= 10:

                    f_current = fc[-1]

                    f_previous = (
                        fc[-9:-1]
                    )

                    f_volume = float(
                        f_current[5]
                    )

                    f_baseline = (
                        safe_median([
                            c[5]
                            for c
                            in f_previous
                        ])
                    )

                    if f_baseline > 0:

                        f_projected = (
                            f_volume /
                            progress
                        )

                        futures_multiple = (
                            f_projected /
                            f_baseline
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

        # -------------------------
        # TEYİTLER
        # -------------------------

        strong_volume = (
            projected_multiple >=
            STRONG_VOLUME_MULTIPLE
        )

        taker_confirmed = (
            taker_ratio >=
            MIN_TAKER_RATIO
        )

        futures_confirmed = (
            futures_multiple
            is not None
            and futures_multiple >=
            MIN_FUTURES_MULTIPLE
        )

        oi_confirmed = (
            oi_change
            is not None
            and oi_change >=
            MIN_OI_CHANGE
        )

        # Fiyatın yukarı yönlü
        # olması ek kalite filtresi
        momentum_confirmed = (
            move_5m > 0
            and (
                move_1m is None
                or move_1m > -0.5
            )
        )

        confirmations = sum([
            strong_volume,
            taker_confirmed,
            futures_confirmed,
            oi_confirmed,
            momentum_confirmed
        ])

        # -------------------------
        # ALARM KALİTESİ
        # -------------------------

        level = "🟡 ERKEN AV"

        if confirmations >= 3:

            level = (
                "🟠 GUCLU ERKEN SINYAL"
            )

        if confirmations >= 4:

            level = (
                "🚨 ERKEN ANOMALI"
            )

        # Zayıf sinyali Telegram'a
        # taşımayalım.
        if confirmations < 2:
            return None

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
                confirmations
        }

    except Exception as e:

        print(
            symbol,
            "derin analiz hatasi:",
            e
        )

        return None


# =========================================================
# ALERT FORMAT
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
        f"{fmt(x['move_15m'], '%')}\n"

        f"🔥 Hacim şimdi: "
        f"{x['current_multiple']:.1f}x\n"

        f"🚀 5dk hacim hız tahmini: "
        f"{x['projected_multiple']:.1f}x\n"

        f"🟢 Spot taker alış: "
        f"%{x['taker_ratio']:.1f}\n"

        f"⚙️ Futures hacim: "
        f"{futures_text}\n"

        f"📊 OI ~10dk: "
        f"{oi_text}\n"

        f"🎯 Breakout: "
        f"{breakout_text}\n"

        f"📍 Breakout mesafe: "
        f"{breakout_distance}\n\n"

        f"⚠️ Erken radar sinyalidir; "
        f"otomatik alim sinyali degildir."
    )


# =========================================================
# MAIN
# =========================================================

def main():

    start = time.time()

    print(
        "Crypto Av Radari v3 basladi."
    )

    symbols = spot_symbols()

    print(
        "Taranacak USDT spot:",
        len(symbols)
    )

    # Futures 451 verirse
    # spot radar devam eder.
    futures_set = futures_symbols()

    print(
        "Futures sembol:",
        len(futures_set)
    )

    # =====================================================
    # HIZLI ÖN TARAMA
    # =====================================================

    candidates = []

    # Aynı anda birkaç istek.
    # 492 coini tek tek beklemiyoruz.
    with ThreadPoolExecutor(
        max_workers=12
    ) as executor:

        futures = {
            executor.submit(
                pre_scan,
                symbol
            ): symbol
            for symbol in symbols
        }

        for future in as_completed(
            futures
        ):

            try:

                result = (
                    future.result()
                )

                if result:

                    candidates.append(
                        result
                    )

            except Exception:
                pass

    candidates.sort(
        key=lambda x:
        x["projected_multiple"],
        reverse=True
    )

    # En güçlü adayları derin tara
    candidates = (
        candidates[
            :MAX_DEEP_SCAN
        ]
    )

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
            alerts.append(result)

    # Önce teyit,
    # sonra hacim ivmesi
    alerts.sort(
        key=lambda x: (
            x["score"],
            x["projected_multiple"]
        ),
        reverse=True
    )

    elapsed = (
        time.time() - start
    )

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

        message = (
            format_alert(alert)
        )

        telegram(message)

        print(
            alert["symbol"],
            alert["level"],
            "spot:",
            f"{alert['projected_multiple']:.1f}x",
            "taker:",
            f"%{alert['taker_ratio']:.1f}",
            "score:",
            alert["score"]
        )


if __name__ == "__main__":
    main()
