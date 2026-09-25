import os
import time
import requests
from statistics import median

# =========================
# BAGLANTILAR
# =========================

SPOT = "https://data-api.binance.vision"
FUTURES = "https://fapi.binance.com"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# =========================
# RADAR AYARLARI
# =========================

# Acik 5dk mumunda erken uyari
EARLY_VOLUME_MULTIPLE = 3.0

# Guclu hacim anomalisi
STRONG_VOLUME_MULTIPLE = 5.0

# Coktan ucmus coinleri kovalamamak icin
MAX_PRICE_MOVE_5M = 12.0

# Taker alici teyidi
MIN_TAKER_RATIO = 55.0

# OI teyidi
MIN_OI_CHANGE = 2.0

EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI",
    "EUR", "TRY", "BRL", "GBP", "JPY", "AUD",
    "BIDR"
}

session = requests.Session()
session.headers.update({
    "User-Agent": "Crypto-Av-Radari/2.0"
})


# =========================
# HTTP
# =========================

def get_json(url, params=None, timeout=10):
    r = session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


# =========================
# TELEGRAM
# =========================

def telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram secret eksik.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    try:
        requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=10
        ).raise_for_status()

    except Exception as e:
        print("Telegram hatasi:", e)


# =========================
# SYMBOL LISTELERI
# =========================

def spot_symbols():
    info = get_json(f"{SPOT}/api/v3/exchangeInfo")

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


def futures_symbols():

    try:
        info = get_json(
            f"{FUTURES}/fapi/v1/exchangeInfo"
        )

        return {
            s["symbol"]
            for s in info["symbols"]
            if s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
            and s.get("contractType") == "PERPETUAL"
        }

    except Exception as e:

        # Binance Futures GitHub Actions IP'sini
        # engellerse radar artik COKMEYECEK.
        print("Futures API kullanilamiyor:", e)
        print("Spot radar devam ediyor.")

        return set()


# =========================
# KLINE
# =========================

def klines(symbol, interval="5m", limit=20):

    return get_json(
        f"{SPOT}/api/v3/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
    )


# =========================
# FUTURES / OI
# =========================

def oi_history(symbol):

    return get_json(
        f"{FUTURES}/futures/data/openInterestHist",
        {
            "symbol": symbol,
            "period": "5m",
            "limit": 4
        }
    )


def futures_klines(symbol):

    return get_json(
        f"{FUTURES}/fapi/v1/klines",
        {
            "symbol": symbol,
            "interval": "5m",
            "limit": 12
        }
    )


# =========================
# YARDIMCI
# =========================

def pct(a, b):

    if a == 0:
        return 0

    return ((b / a) - 1) * 100


# =========================
# COIN TARAMA
# =========================

def scan_symbol(symbol, futures_set):

    candles = klines(
        symbol,
        interval="5m",
        limit=20
    )

    if len(candles) < 12:
        return None

    # Son eleman ACIK 5dk mumudur
    current = candles[-1]

    # Onceki 8 KAPANMIS mum
    previous = candles[-9:-1]

    current_open = float(current[1])
    current_close = float(current[4])
    current_volume = float(current[5])
    taker_buy_volume = float(current[9])

    baseline_volumes = [
        float(c[5])
        for c in previous
    ]

    baseline = median(baseline_volumes)

    if baseline <= 0:
        return None

    # -------------------------
    # ACIK MUM SURE DUZELTMESI
    # -------------------------

    open_time = int(current[0])

    elapsed_seconds = (
        int(time.time() * 1000) - open_time
    ) / 1000

    elapsed_seconds = max(
        15,
        min(elapsed_seconds, 300)
    )

    progress = elapsed_seconds / 300

    # Mum bu hizla devam ederse
    # 5dk sonunda tahmini hacim
    projected_volume = (
        current_volume / progress
    )

    current_multiple = (
        current_volume / baseline
    )

    projected_multiple = (
        projected_volume / baseline
    )

    price_move = pct(
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
    # 1DK MOMENTUM
    # -------------------------

    one_min = klines(
        symbol,
        interval="1m",
        limit=3
    )

    move_1m = None

    if one_min:

        m = one_min[-1]

        move_1m = pct(
            float(m[1]),
            float(m[4])
        )

    # -------------------------
    # 15DK MOMENTUM
    # -------------------------

    fifteen = klines(
        symbol,
        interval="15m",
        limit=2
    )

    move_15m = None

    if fifteen:

        m = fifteen[-1]

        move_15m = pct(
            float(m[1]),
            float(m[4])
        )

    # -------------------------
    # ERKEN HACIM FILTRESI
    # -------------------------

    if projected_multiple < EARLY_VOLUME_MULTIPLE:
        return None

    # Asiri yukselmis 5dk mumu
    if price_move > MAX_PRICE_MOVE_5M:
        return None

    # Negatif momentumda pump alarmi verme
    if price_move < -2:
        return None

    oi_change = None
    futures_multiple = None

    # -------------------------
    # FUTURES TEYIDI
    # -------------------------

    if symbol in futures_set:

        try:

            oi = oi_history(symbol)

            if len(oi) >= 3:

                old_oi = float(
                    oi[-3]["sumOpenInterest"]
                )

                new_oi = float(
                    oi[-1]["sumOpenInterest"]
                )

                oi_change = pct(
                    old_oi,
                    new_oi
                )

            fc = futures_klines(symbol)

            if len(fc) >= 10:

                f_current = fc[-1]

                f_previous = fc[-9:-1]

                f_volume = float(
                    f_current[5]
                )

                f_baseline = median([
                    float(c[5])
                    for c in f_previous
                ])

                if f_baseline > 0:

                    f_projected = (
                        f_volume / progress
                    )

                    futures_multiple = (
                        f_projected /
                        f_baseline
                    )

        except Exception as e:

            print(
                symbol,
                "futures teyidi kullanilamadi:",
                e
            )

    # -------------------------
    # PUAN / SEVIYE
    # -------------------------

    level = "🟡 ERKEN AV"

    strong = (
        projected_multiple >=
        STRONG_VOLUME_MULTIPLE
    )

    taker_confirmed = (
        taker_ratio >=
        MIN_TAKER_RATIO
    )

    futures_confirmed = (
        futures_multiple is not None
        and futures_multiple >= 3
    )

    oi_confirmed = (
        oi_change is not None
        and oi_change >= MIN_OI_CHANGE
    )

    confirmations = sum([
        strong,
        taker_confirmed,
        futures_confirmed,
        oi_confirmed
    ])

    if confirmations >= 2:
        level = "🟠 GUCLU ERKEN SINYAL"

    if confirmations >= 3:
        level = "🚨 ERKEN ANOMALI"

    return {
        "symbol": symbol,
        "price": current_close,
        "move_1m": move_1m,
        "move_5m": price_move,
        "move_15m": move_15m,
        "current_multiple": current_multiple,
        "projected_multiple": projected_multiple,
        "taker_ratio": taker_ratio,
        "futures_multiple": futures_multiple,
        "oi_change": oi_change,
        "level": level,
        "score": confirmations
    }


# =========================
# FORMAT
# =========================

def fmt(value, suffix="", digits=2):

    if value is None:
        return "-"

    return (
        f"{value:+.{digits}f}{suffix}"
    )


def format_alert(x):

    futures_text = (
        f"{x['futures_multiple']:.1f}x"
        if x["futures_multiple"] is not None
        else "-"
    )

    oi_text = (
        fmt(x["oi_change"], "%")
        if x["oi_change"] is not None
        else "-"
    )

    return (
        f"{x['level']}\n\n"
        f"🪙 {x['symbol']}\n"
        f"💵 Fiyat: {x['price']}\n"
        f"⚡ 1dk: {fmt(x['move_1m'], '%')}\n"
        f"📈 5dk: {fmt(x['move_5m'], '%')}\n"
        f"📊 15dk: {fmt(x['move_15m'], '%')}\n"
        f"🔥 5dk hacim simdi: "
        f"{x['current_multiple']:.1f}x\n"
        f"🚀 5dk hacim hiz tahmini: "
        f"{x['projected_multiple']:.1f}x\n"
        f"🟢 Spot taker alici: "
        f"%{x['taker_ratio']:.1f}\n"
        f"⚙️ Futures hacim: "
        f"{futures_text}\n"
        f"📊 OI ~10dk: "
        f"{oi_text}\n\n"
        f"⚠️ Erken radar sinyalidir; "
        f"otomatik alim sinyali degildir."
    )


# =========================
# MAIN
# =========================

def main():

    print("Crypto Av Radari v2 basladi.")

    symbols = spot_symbols()

    print(
        "Taranacak USDT spot paritesi:",
        len(symbols)
    )

    futures_set = futures_symbols()

    print(
        "Futures teyidi bulunan sembol:",
        len(futures_set)
    )

    alerts = []

    for index, symbol in enumerate(symbols, 1):

        try:

            result = scan_symbol(
                symbol,
                futures_set
            )

            if result:
                alerts.append(result)

            time.sleep(0.05)

        except Exception as e:

            print(
                symbol,
                "tarama hatasi:",
                e
            )

    # Once teyit sayisi,
    # sonra hacim ivmesi
    alerts.sort(
        key=lambda x: (
            x["score"],
            x["projected_multiple"]
        ),
        reverse=True
    )

    if not alerts:

        print(
            "Yeni anlamli erken sinyal yok."
        )

        return

    print(
        "Alarm adayi:",
        len(alerts)
    )

    # Spam olmamasi icin
    # en guclu 5 sinyal
    for alert in alerts[:5]:

        message = format_alert(alert)

        telegram(message)

        print(
            alert["symbol"],
            alert["level"],
            f"{alert['projected_multiple']:.1f}x"
        )


if __name__ == "__main__":
    main()
