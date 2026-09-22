import os
import time
import requests
from statistics import median

SPOT = "https://data-api.binance.vision"
FUTURES = "https://fapi.binance.com"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Radar ayarlari
MIN_VOLUME_MULTIPLE = 4.0
STRONG_VOLUME_MULTIPLE = 5.0
MIN_PRICE_MOVE = 2.0
MAX_PRICE_MOVE = 12.0
MIN_OI_CHANGE = 2.0

# Stablecoin / TradFi benzeri varliklari ele
EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "AEUR",
    "TRY", "BRL", "GBP", "JPY", "AUD", "BIDR", "IDRT"
}

session = requests.Session()
session.headers.update({"User-Agent": "CryptoAvRadari/1.0"})


def get_json(url, params=None, timeout=10):
    r = session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


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
    info = get_json(f"{FUTURES}/fapi/v1/exchangeInfo")

    return {
        s["symbol"]
        for s in info["symbols"]
        if s["quoteAsset"] == "USDT"
        and s["status"] == "TRADING"
        and s.get("contractType") == "PERPETUAL"
    }


def klines(symbol, limit=14):
    return get_json(
        f"{SPOT}/api/v3/klines",
        {
            "symbol": symbol,
            "interval": "5m",
            "limit": limit
        }
    )


def oi_history(symbol):
    return get_json(
        f"{FUTURES}/futures/data/openInterestHist",
        {
            "symbol": symbol,
            "period": "5m",
            "limit": 4
        }
    )


def funding(symbol):
    data = get_json(
        f"{FUTURES}/fapi/v1/premiumIndex",
        {"symbol": symbol}
    )
    return float(data.get("lastFundingRate", 0)) * 100


def scan_symbol(symbol, futures_set):
    candles = klines(symbol)

    if len(candles) < 12:
        return None

    # Son kapanmis 5dk mum
    current = candles[-2]

    # Onceki 8 kapanmis mum
    previous = candles[-10:-2]

    current_open = float(current[1])
    current_close = float(current[4])
    current_volume = float(current[5])
    taker_buy_volume = float(current[9])

    baseline_volumes = [float(c[5]) for c in previous]
    baseline = median(baseline_volumes)

    if baseline <= 0:
        return None

    volume_multiple = current_volume / baseline
    price_move = ((current_close / current_open) - 1) * 100

    taker_ratio = (
        taker_buy_volume / current_volume * 100
        if current_volume > 0 else 0
    )

    # Ilk filtre: fiyat + hacim ateslemesi
    if volume_multiple < MIN_VOLUME_MULTIPLE:
        return None

    if price_move < MIN_PRICE_MOVE:
        return None

    if price_move > MAX_PRICE_MOVE:
        return None

    oi_change = None
    funding_rate = None

    if symbol in futures_set:
        try:
            oi = oi_history(symbol)

            if len(oi) >= 3:
                old_oi = float(oi[-3]["sumOpenInterest"])
                new_oi = float(oi[-1]["sumOpenInterest"])

                if old_oi > 0:
                    oi_change = ((new_oi / old_oi) - 1) * 100

            funding_rate = funding(symbol)

        except Exception as e:
            print(symbol, "futures verisi alinamadi:", e)

    level = "🟡 ERKEN AV"

    # CELR / CHR tipi turev liderli anomali
    if (
        volume_multiple >= STRONG_VOLUME_MULTIPLE
        and oi_change is not None
        and oi_change >= MIN_OI_CHANGE
    ):
        level = "🚨 ERKEN ANOMALİ — türev liderli, spot teyidi eksik olabilir"

    # Spot teyidi de gelirse
    if (
        volume_multiple >= STRONG_VOLUME_MULTIPLE
        and oi_change is not None
        and oi_change >= MIN_OI_CHANGE
        and taker_ratio >= 55
    ):
        level = "🟢 TEYİTLİ"

    return {
        "symbol": symbol,
        "price": current_close,
        "move": price_move,
        "volume_multiple": volume_multiple,
        "taker_ratio": taker_ratio,
        "oi_change": oi_change,
        "funding": funding_rate,
        "level": level
    }


def format_alert(x):
    oi_text = (
        f"{x['oi_change']:+.2f}%"
        if x["oi_change"] is not None
        else "Futures/OI yok"
    )

    funding_text = (
        f"{x['funding']:+.4f}%"
        if x["funding"] is not None
        else "—"
    )

    leveraged = ""

    if x["oi_change"] is not None and x["oi_change"] >= MIN_OI_CHANGE:
        leveraged = (
            "\n⚡ Fiyat + hacim + OI birlikte artıyor:"
            " yeni kaldıraçlı pozisyon girişi olası."
        )

    return (
        f"{x['level']}\n\n"
        f"🪙 {x['symbol']}\n"
        f"💵 Fiyat: {x['price']}\n"
        f"📈 Son 5dk: {x['move']:+.2f}%\n"
        f"🔥 5dk hacim: {x['volume_multiple']:.1f}x\n"
        f"🟢 Spot taker alıcı: %{x['taker_ratio']:.1f}\n"
        f"📊 OI ~10dk: {oi_text}\n"
        f"💰 Funding: {funding_text}"
        f"{leveraged}\n\n"
        f"⚠️ Radar sinyalidir, otomatik alım değildir."
    )


def main():
    print("Crypto Av Radari basladi.")

    symbols = spot_symbols()
    futures_set = futures_symbols()

    print("Taranacak USDT paritesi:", len(symbols))

    alerts = []

    for symbol in symbols:
        try:
            result = scan_symbol(symbol, futures_set)

            if result:
                alerts.append(result)

            # Binance'a gereksiz yuk bindirmemek icin
            time.sleep(0.04)

        except Exception as e:
            print(symbol, "hata:", e)

    # En guclu hacim anomalileri once
    alerts.sort(
        key=lambda x: x["volume_multiple"],
        reverse=True
    )

    if not alerts:
        print("Yeni anlamli erken sinyal yok.")
        return

    # Bir calismada maksimum 5 alarm
    for alert in alerts[:5]:
        telegram(format_alert(alert))
        print(alert["symbol"], alert["level"])


if __name__ == "__main__":
    main()
