import os
import time
import requests
from statistics import median

# =========================================================
# CRYPTO AV RADARI v3
# Spot erken hacim + alici baskisi + buyuk islem izi
# Futures/OI varsa ekstra teyit
# =========================================================

SPOT = "https://data-api.binance.vision"
FUTURES = "https://fapi.binance.com"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# =========================================================
# RADAR AYARLARI
# =========================================================

# Erken hacim
EARLY_VOLUME_MULTIPLE = 2.5

# Guclu hacim
STRONG_VOLUME_MULTIPLE = 5.0

# Spot taker alici baskisi
MIN_TAKER_RATIO = 58.0
STRONG_TAKER_RATIO = 65.0

# Futures hacim teyidi
MIN_FUTURES_MULTIPLE = 2.5

# OI 5-10dk teyidi
MIN_OI_CHANGE = 2.0

# Coin zaten cok ucmussa kovalamiyoruz
MAX_PRICE_MOVE_5M = 8.0
MAX_PRICE_MOVE_15M = 15.0

# Mumun ilk saniyelerinde projeksiyon yapma
MIN_CANDLE_AGE_SECONDS = 45

# Minimum gerceklesmis hacim orani
MIN_CURRENT_VOLUME_MULTIPLE = 0.35

# 1dk hacim hizlanmasi
MIN_1M_VOLUME_MULTIPLE = 2.0

# Buyuk/agresif alim izi
MIN_BUY_TRADE_DOMINANCE = 60.0

EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI",
    "EUR", "TRY", "BRL", "GBP", "JPY", "AUD",
    "BIDR", "USDS", "AEUR", "EURI"
}

session = requests.Session()
session.headers.update({
    "User-Agent": "Crypto-Av-Radari/3.0"
})


# =========================================================
# HTTP
# =========================================================

def get_json(url, params=None, timeout=10):

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
            timeout=10
        ).raise_for_status()

    except Exception as e:

        print(
            "Telegram hatasi:",
            e
        )


# =========================================================
# SYMBOL LISTELERI
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


def futures_symbols():

    try:

        info = get_json(
            f"{FUTURES}/fapi/v1/exchangeInfo"
        )

        result = {
            s["symbol"]
            for s in info["symbols"]
            if (
                s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"
                and s.get("contractType")
                == "PERPETUAL"
            )
        }

        return result

    except Exception as e:

        print(
            "Futures API kullanilamiyor:",
            e
        )

        print(
            "Spot radar bagimsiz devam ediyor."
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
# SPOT AGGREGATE TRADES
# =========================================================

def recent_agg_trades(symbol, limit=500):

    return get_json(
        f"{SPOT}/api/v3/aggTrades",
        {
            "symbol": symbol,
            "limit": limit
        }
    )


# =========================================================
# FUTURES
# =========================================================

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


# =========================================================
# YARDIMCI
# =========================================================

def pct(a, b):

    if a == 0:
        return 0

    return (
        (b / a) - 1
    ) * 100


def safe_median(values):

    values = [
        x for x in values
        if x > 0
    ]

    if not values:
        return 0

    return median(values)


# =========================================================
# BUY / WHALE PRESSURE
# =========================================================

def trade_pressure(symbol):

    try:

        trades = recent_agg_trades(
            symbol,
            limit=300
        )

        if not trades:
            return None

        buy_quote = 0
        sell_quote = 0

        buy_sizes = []

        for t in trades:

            price = float(t["p"])
            qty = float(t["q"])

            quote_value = (
                price * qty
            )

            # m = True:
            # buyer maker -> aggressive sell
            #
            # m = False:
            # seller maker -> aggressive buy

            if not t["m"]:

                buy_quote += (
                    quote_value
                )

                buy_sizes.append(
                    quote_value
                )

            else:

                sell_quote += (
                    quote_value
                )

        total = (
            buy_quote +
            sell_quote
        )

        if total <= 0:
            return None

        buy_ratio = (
            buy_quote /
            total *
            100
        )

        biggest_buy = (
            max(buy_sizes)
            if buy_sizes
            else 0
        )

        return {
            "buy_ratio":
                buy_ratio,

            "buy_quote":
                buy_quote,

            "sell_quote":
                sell_quote,

            "biggest_buy":
                biggest_buy
        }

    except Exception:

        return None


# =========================================================
# BREAKOUT
# =========================================================

def breakout_level(candles):

    # Acik mum haric
    previous = candles[-13:-1]

    if not previous:
        return None

    highs = [
        float(c[2])
        for c in previous
    ]

    return max(highs)


# =========================================================
# COIN TARAMA
# =========================================================

def scan_symbol(
    symbol,
    futures_set
):

    candles = klines(
        symbol,
        interval="5m",
        limit=25
    )

    if len(candles) < 15:
        return None

    current = candles[-1]

    # Onceki 10 kapanmis 5dk mum
    previous = candles[-11:-1]

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
        float(c[5])
        for c in previous
    ])

    if baseline <= 0:
        return None

    # =====================================================
    # ACIK 5DK MUMUN YASI
    # =====================================================

    open_time = int(
        current[0]
    )

    elapsed_seconds = (
        int(time.time() * 1000)
        - open_time
    ) / 1000

    elapsed_seconds = max(
        1,
        min(
            elapsed_seconds,
            300
        )
    )

    # Mum cok yeniyse
    # sahte 50x-100x tahmini yapma
    if (
        elapsed_seconds
        < MIN_CANDLE_AGE_SECONDS
    ):
        return None

    progress = (
        elapsed_seconds /
        300
    )

    current_multiple = (
        current_volume /
        baseline
    )

    projected_volume = (
        current_volume /
        progress
    )

    projected_multiple = (
        projected_volume /
        baseline
    )

    # =====================================================
    # FIYAT
    # =====================================================

    move_5m = pct(
        current_open,
        current_close
    )

    taker_ratio = (
        taker_buy_volume /
        current_volume *
        100
        if current_volume > 0
        else 0
    )

    # =====================================================
    # 1DK
    # =====================================================

    one_min = klines(
        symbol,
        interval="1m",
        limit=12
    )

    move_1m = None
    volume_1m_multiple = None

    if len(one_min) >= 10:

        m = one_min[-1]

        move_1m = pct(
            float(m[1]),
            float(m[4])
        )

        current_1m_volume = float(
            m[5]
        )

        baseline_1m = safe_median([
            float(x[5])
            for x in one_min[-9:-1]
        ])

        if baseline_1m > 0:

            volume_1m_multiple = (
                current_1m_volume /
                baseline_1m
            )

    # =====================================================
    # 15DK
    # =====================================================

    fifteen = klines(
        symbol,
        interval="15m",
        limit=5
    )

    move_15m = None

    if fifteen:

        m = fifteen[-1]

        move_15m = pct(
            float(m[1]),
            float(m[4])
        )

    # =====================================================
    # FIYAT ZATEN KACTI MI?
    # =====================================================

    if (
        move_5m >
        MAX_PRICE_MOVE_5M
    ):
        return None

    if (
        move_15m is not None
        and move_15m >
        MAX_PRICE_MOVE_15M
    ):
        return None

    # Sert negatif hareketi
    # pump diye bildirme
    if move_5m < -2:
        return None

    # =====================================================
    # HACIM FILTRESI
    # =====================================================

    early_volume = (
        projected_multiple
        >= EARLY_VOLUME_MULTIPLE
    )

    real_volume_present = (
        current_multiple
        >= MIN_CURRENT_VOLUME_MULTIPLE
    )

    one_min_acceleration = (
        volume_1m_multiple is not None
        and volume_1m_multiple
        >= MIN_1M_VOLUME_MULTIPLE
    )

    # Tahmin tek basina yeterli degil.
    if not early_volume:
        return None

    if not (
        real_volume_present
        or one_min_acceleration
    ):
        return None

    # =====================================================
    # TRADE / BUY PRESSURE
    # =====================================================

    pressure = trade_pressure(
        symbol
    )

    aggressive_buy_ratio = None
    biggest_buy = None

    if pressure:

        aggressive_buy_ratio = (
            pressure["buy_ratio"]
        )

        biggest_buy = (
            pressure["biggest_buy"]
        )

    aggressive_buy_confirmed = (
        aggressive_buy_ratio
        is not None
        and aggressive_buy_ratio
        >= MIN_BUY_TRADE_DOMINANCE
    )

    # =====================================================
    # FUTURES + OI
    # =====================================================

    futures_multiple = None
    oi_change = None

    futures_available = (
        symbol in futures_set
    )

    if futures_available:

        try:

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

            fc = futures_klines(
                symbol
            )

            if len(fc) >= 10:

                f_current = (
                    fc[-1]
                )

                f_previous = (
                    fc[-9:-1]
                )

                f_volume = float(
                    f_current[5]
                )

                f_baseline = (
                    safe_median([
                        float(c[5])
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

        except Exception as e:

            print(
                symbol,
                "futures teyidi "
                "kullanilamadi:",
                e
            )

    # =====================================================
    # TEYITLER
    # =====================================================

    strong_volume = (
        projected_multiple
        >= STRONG_VOLUME_MULTIPLE
    )

    spot_taker_confirmed = (
        taker_ratio
        >= MIN_TAKER_RATIO
    )

    strong_taker = (
        taker_ratio
        >= STRONG_TAKER_RATIO
    )

    futures_confirmed = (
        futures_multiple
        is not None
        and futures_multiple
        >= MIN_FUTURES_MULTIPLE
    )

    oi_confirmed = (
        oi_change
        is not None
        and oi_change
        >= MIN_OI_CHANGE
    )

    # =====================================================
    # PUAN
    # =====================================================

    score = 0

    if strong_volume:
        score += 2

    if spot_taker_confirmed:
        score += 1

    if strong_taker:
        score += 1

    if one_min_acceleration:
        score += 1

    if aggressive_buy_confirmed:
        score += 2

    if futures_confirmed:
        score += 2

    if oi_confirmed:
        score += 2

    # =====================================================
    # SEVIYE
    # =====================================================

    level = "🟡 IZLE"

    # Hacim + alici tarafinda
    # en az bir gercek teyit olmadan
    # guclu alarm yok
    buyer_confirmation = (
        spot_taker_confirmed
        or aggressive_buy_confirmed
    )

    if (
        score >= 4
        and buyer_confirmation
    ):

        level = (
            "🟠 ERKEN ATESLEME"
        )

    # Futures/OI yoksa da olabilir,
    # fakat spot tarafinin cok guclu
    # olmasi gerekir.
    strong_spot_setup = (
        strong_volume
        and strong_taker
        and aggressive_buy_confirmed
        and one_min_acceleration
    )

    derivative_confirmation = (
        futures_confirmed
        or oi_confirmed
    )

    if (
        score >= 7
        and buyer_confirmation
        and (
            derivative_confirmation
            or strong_spot_setup
        )
    ):

        level = (
            "🚨 GUCLU ANOMALI"
        )

    # Sadece IZLE seviyesini
    # Telegram'a yollamiyoruz.
    if level == "🟡 IZLE":
        return None

    breakout = breakout_level(
        candles
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

        "volume_1m_multiple":
            volume_1m_multiple,

        "taker_ratio":
            taker_ratio,

        "aggressive_buy_ratio":
            aggressive_buy_ratio,

        "biggest_buy":
            biggest_buy,

        "futures_multiple":
            futures_multiple,

        "oi_change":
            oi_change,

        "futures_available":
            futures_available,

        "breakout":
            breakout,

        "level":
            level,

        "score":
            score
    }


# =========================================================
# FORMAT
# =========================================================

def fmt(
    value,
    suffix="",
    digits=2
):

    if value is None:
        return "-"

    return (
        f"{value:+.{digits}f}"
        f"{suffix}"
    )


def format_price(value):

    if value is None:
        return "-"

    return (
        f"{value:.10f}"
        .rstrip("0")
        .rstrip(".")
    )


def format_alert(x):

    futures_text = "-"

    if x["futures_multiple"] is not None:

        futures_text = (
            f"{x['futures_multiple']:.1f}x"
        )

    elif not x["futures_available"]:

        futures_text = (
            "erisilemedi"
        )

    oi_text = "-"

    if x["oi_change"] is not None:

        oi_text = fmt(
            x["oi_change"],
            "%"
        )

    elif not x["futures_available"]:

        oi_text = (
            "erisilemedi"
        )

    one_volume = "-"

    if (
        x["volume_1m_multiple"]
        is not None
    ):

        one_volume = (
            f"{x['volume_1m_multiple']:.1f}x"
        )

    aggressive = "-"

    if (
        x["aggressive_buy_ratio"]
        is not None
    ):

        aggressive = (
            f"%"
            f"{x['aggressive_buy_ratio']:.1f}"
        )

    whale = "-"

    if x["biggest_buy"] is not None:

        whale = (
            f"${x['biggest_buy']:,.0f}"
        )

    breakout = (
        format_price(
            x["breakout"]
        )
        if x["breakout"]
        is not None
        else "-"
    )

    return (
        f"{x['level']}\n\n"

        f"🪙 {x['symbol']}\n"

        f"💵 Fiyat: "
        f"{format_price(x['price'])}\n"

        f"⚡ 1dk: "
        f"{fmt(x['move_1m'], '%')}\n"

        f"📈 5dk: "
        f"{fmt(x['move_5m'], '%')}\n"

        f"📊 15dk: "
        f"{fmt(x['move_15m'], '%')}\n\n"

        f"🔥 5dk hacim gercek: "
        f"{x['current_multiple']:.1f}x\n"

        f"🚀 5dk hacim hiz tahmini: "
        f"{x['projected_multiple']:.1f}x\n"

        f"⚡ 1dk hacim: "
        f"{one_volume}\n"

        f"🟢 Spot taker: "
        f"%{x['taker_ratio']:.1f}\n"

        f"🐋 Agresif alis: "
        f"{aggressive}\n"

        f"💰 En buyuk son alis: "
        f"{whale}\n\n"

        f"⚙️ Futures hacim: "
        f"{futures_text}\n"

        f"📊 OI ~10dk: "
        f"{oi_text}\n"

        f"🎯 Breakout: "
        f"{breakout}\n\n"

        f"Radar puani: "
        f"{x['score']}\n"

        f"⚠️ Erken anomali radaridir; "
        f"otomatik alim sinyali degildir."
    )


# =========================================================
# MAIN
# =========================================================

def main():

    print(
        "Crypto Av Radari v3 basladi."
    )

    symbols = spot_symbols()

    print(
        "Taranacak USDT spot:",
        len(symbols)
    )

    futures_set = (
        futures_symbols()
    )

    print(
        "Futures erisilebilen sembol:",
        len(futures_set)
    )

    if not futures_set:

        print(
            "UYARI: Futures/OI teyidi "
            "bu calismada kullanilamayacak."
        )

    alerts = []

    for symbol in symbols:

        try:

            result = scan_symbol(
                symbol,
                futures_set
            )

            if result:

                alerts.append(
                    result
                )

            time.sleep(0.04)

        except Exception as e:

            print(
                symbol,
                "tarama hatasi:",
                e
            )

    alerts.sort(
        key=lambda x: (
            x["score"],
            x["projected_multiple"]
        ),
        reverse=True
    )

    if not alerts:

        print(
            "Yeni anlamli erken "
            "atesleme yok."
        )

        return

    print(
        "Guclu alarm adayi:",
        len(alerts)
    )

    # En guclu 5
    for alert in alerts[:5]:

        message = (
            format_alert(alert)
        )

        telegram(
            message
        )

        print(
            alert["symbol"],
            alert["level"],
            "score:",
            alert["score"],
            "hacim:",
            f"{alert['projected_multiple']:.1f}x"
        )


if __name__ == "__main__":
    main()
