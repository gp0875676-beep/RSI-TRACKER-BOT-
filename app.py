"""
╔══════════════════════════════════════════════════════════╗
║   CRYPTO RSI + MFI TELEGRAM BOT — MILITARY GRADE       ║
║   Every 1 min updates | 5m 1H 4H | Volume Spike        ║
║   Any coin | /stop to halt | Render ready              ║
╚══════════════════════════════════════════════════════════╝
"""

import os, time, threading, logging
from datetime import datetime, timezone
import requests
import pandas as pd
import numpy as np
from flask import Flask, request, jsonify

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── ENV VARIABLES (Render pe set karo) ──
BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")   # e.g. https://your-app.onrender.com

app = Flask(__name__)

# ══════════════════════════════════════════
#  CONSTANTS
# ══════════════════════════════════════════
COINGECKO_BASE  = "https://api.coingecko.com/api/v3"
RSI_PERIOD      = 14
MFI_PERIOD      = 14
VOLUME_SPIKE_X  = 2.0    # 2x average volume = spike
UPDATE_INTERVAL = 60      # seconds between each update

TIMEFRAMES = {
    "5m":  {"days": 1,  "label": "5 Min",  "candles": 288},
    "1H":  {"days": 30, "label": "1 Hour", "candles": 168},
    "4H":  {"days": 90, "label": "4 Hour", "candles": 90},
}

# ── Tracked sessions: {chat_id: {coin: thread_stop_event}} ──
active_sessions: dict[int, dict[str, threading.Event]] = {}
session_lock = threading.Lock()

# ── Coin search cache ──
coin_cache: dict[str, str] = {}

KNOWN_COINS = {
    "BTC":"bitcoin","ETH":"ethereum","SOL":"solana","BNB":"binancecoin",
    "XRP":"ripple","ADA":"cardano","DOGE":"dogecoin","AVAX":"avalanche-2",
    "DOT":"polkadot","MATIC":"matic-network","LINK":"chainlink","UNI":"uniswap",
    "LTC":"litecoin","ATOM":"cosmos","NEAR":"near","APT":"aptos","ARB":"arbitrum",
    "OP":"optimism","SUI":"sui","PEPE":"pepe","WIF":"dogwifcoin","SHIB":"shiba-inu",
    "TON":"the-open-network","TRX":"tron","FIL":"filecoin","AAVE":"aave",
    "INJ":"injective-protocol","SEI":"sei-network","HBAR":"hedera-hashgraph",
    "VET":"vechain","BONK":"bonk","WLD":"worldcoin-wld","RENDER":"render-token",
    "JUP":"jupiter-exchange-solana","PYTH":"pyth-network","JTO":"jito-governance-token",
    "ONDO":"ondo-finance","STRK":"starknet","MANTA":"manta-network","ALT":"altlayer",
    "DYM":"dymension","TIA":"celestia","BLUR":"blur","GMX":"gmx","GRT":"the-graph",
    "LDO":"lido-dao","RUNE":"thorchain","SNX":"synthetix-network-token","CRV":"curve-dao-token",
    "MKR":"maker","COMP":"compound-governance-token","BAL":"balancer","SUSHI":"sushi",
    "1INCH":"1inch","ENS":"ethereum-name-service","IMX":"immutable-x","MANA":"decentraland",
    "SAND":"the-sandbox","AXS":"axie-infinity","GALA":"gala","CHZ":"chiliz",
    "FLOW":"flow","EGLD":"elrond-erd-2","THETA":"theta-token","FTM":"fantom",
    "KAVA":"kava","ZIL":"zilliqa","ICX":"icon","ZEC":"zcash","XMR":"monero",
    "DASH":"dash","ETC":"ethereum-classic","BCH":"bitcoin-cash","BSV":"bitcoin-sv",
    "NEO":"neo","WAVES":"waves","XTZ":"tezos","ALGO":"algorand","IOTA":"iota",
    "HOT":"holotoken","ZRX":"0x","BAT":"basic-attention-token","ENJ":"enjincoin",
    "ANKR":"ankr","CELR":"celer-network","SKL":"skale","STORJ":"storj",
    "USDT":"tether","USDC":"usd-coin","BUSD":"binance-usd","DAI":"dai",
    "TUSD":"true-usd","FRAX":"frax","USDP":"paxos-standard",
}


# ══════════════════════════════════════════
#  TELEGRAM HELPERS
# ══════════════════════════════════════════
def tg_send(chat_id: int, text: str):
    if not BOT_TOKEN:
        log.error("BOT_TOKEN not set!")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
        if r.status_code != 200:
            log.warning(f"Telegram send failed: {r.text[:200]}")
    except Exception as e:
        log.error(f"tg_send error: {e}")


def tg_edit(chat_id: int, message_id: int, text: str):
    """Edit existing message (live update feel)"""
    if not BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML"
            },
            timeout=10
        )
    except Exception as e:
        log.error(f"tg_edit error: {e}")


# ══════════════════════════════════════════
#  COIN RESOLVER
# ══════════════════════════════════════════
def resolve_coin(ticker: str) -> tuple[str, str]:
    """Returns (coin_id, display_name) or raises ValueError"""
    t = ticker.upper().strip()

    # Known list
    if t in KNOWN_COINS:
        cid = KNOWN_COINS[t]
        coin_cache[t] = cid
        return cid, t

    # Cache
    if t in coin_cache:
        return coin_cache[t], t

    # CoinGecko search
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/search",
            params={"query": t},
            timeout=10
        )
        coins = r.json().get("coins", [])

        # Exact symbol match first
        for c in coins:
            if c["symbol"].upper() == t:
                coin_cache[t] = c["id"]
                return c["id"], c["symbol"].upper()

        # First result
        if coins:
            c = coins[0]
            sym = c["symbol"].upper()
            coin_cache[sym] = c["id"]
            return c["id"], sym

    except Exception as e:
        log.error(f"resolve_coin error: {e}")

    raise ValueError(f"'{ticker}' nahi mila. Sahi symbol daalo (e.g. BTC, PEPE, WIF)")


# ══════════════════════════════════════════
#  DATA FETCH
# ══════════════════════════════════════════
def fetch_ohlc(coin_id: str, days: int) -> pd.DataFrame | None:
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/coins/{coin_id}/ohlc",
            params={"vs_currency": "usd", "days": str(days)},
            timeout=15
        )
        if r.status_code == 429:
            log.warning("CoinGecko rate limit hit")
            time.sleep(60)
            return None
        if r.status_code != 200:
            return None
        data = r.json()
        if not data or not isinstance(data, list):
            return None
        df = pd.DataFrame(data, columns=["ts","open","high","low","close"])
        for c in ["open","high","low","close"]:
            df[c] = df[c].astype(float)
        df["volume"] = 1.0
        return df
    except Exception as e:
        log.error(f"fetch_ohlc error: {e}")
        return None


def fetch_volume_series(coin_id: str, days: int) -> pd.Series | None:
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/coins/{coin_id}/market_chart",
            params={"vs_currency": "usd", "days": str(days)},
            timeout=15
        )
        if r.status_code != 200:
            return None
        vols = r.json().get("total_volumes", [])
        if not vols:
            return None
        return pd.DataFrame(vols, columns=["ts","volume"])["volume"]
    except Exception:
        return None


def get_price(coin_id: str) -> float:
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/simple/price",
            params={"ids": coin_id, "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=8
        )
        d = r.json().get(coin_id, {})
        return float(d.get("usd", 0)), float(d.get("usd_24h_change", 0))
    except Exception:
        return 0.0, 0.0


# ══════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════
def calc_rsi(close: pd.Series) -> float:
    if len(close) < RSI_PERIOD + 1:
        return float("nan")
    d = close.diff()
    gain = d.clip(lower=0)
    loss = (-d).clip(lower=0)
    ag = gain.ewm(alpha=1/RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    al = loss.ewm(alpha=1/RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    val = float((100 - 100/(1+rs)).iloc[-1])
    return round(val, 2) if not np.isnan(val) else float("nan")


def calc_mfi(df: pd.DataFrame) -> float:
    if len(df) < MFI_PERIOD + 1:
        return float("nan")
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    rmf = tp * df["volume"]
    prev = tp.shift(1)
    pos = rmf.where(tp > prev, 0).rolling(MFI_PERIOD).sum()
    neg = rmf.where(tp < prev, 0).rolling(MFI_PERIOD).sum()
    val = float((100 - 100/(1 + pos/neg.replace(0, np.nan))).iloc[-1])
    return round(val, 2) if not np.isnan(val) else float("nan")


def detect_volume_spike(vol_series: pd.Series, lookback: int = 20) -> tuple[bool, float]:
    """Returns (is_spike, ratio) — ratio = current / avg"""
    if vol_series is None or len(vol_series) < lookback + 1:
        return False, 0.0
    avg = float(vol_series.iloc[-(lookback+1):-1].mean())
    cur = float(vol_series.iloc[-1])
    if avg == 0:
        return False, 0.0
    ratio = cur / avg
    return ratio >= VOLUME_SPIKE_X, round(ratio, 2)


def rsi_bar(v: float, width: int = 12) -> str:
    if np.isnan(v): return "░" * width
    f = max(0, min(width, int(v/100*width)))
    return "█"*f + "░"*(width-f)


def rsi_label(v: float) -> str:
    if np.isnan(v): return "N/A"
    if v >= 70:     return "🔴 OVERBOUGHT"
    if v <= 30:     return "🟢 OVERSOLD"
    if v >= 60:     return "🟡 Bullish"
    if v <= 40:     return "🟠 Bearish"
    return                 "⚪ Neutral"


def mfi_label(v: float) -> str:
    if np.isnan(v): return "N/A"
    if v >= 80:     return "🔴 OVERBOUGHT"
    if v <= 20:     return "🟢 OVERSOLD"
    if v >= 65:     return "🟡 Buy Pressure"
    if v <= 35:     return "🟠 Sell Pressure"
    return                 "⚪ Neutral"


def fmt_price(p: float) -> str:
    if p == 0:    return "N/A"
    if p >= 1000: return f"${p:,.2f}"
    if p >= 1:    return f"${p:.4f}"
    if p >= 0.01: return f"${p:.6f}"
    return               f"${p:.10f}"


# ══════════════════════════════════════════
#  BUILD MESSAGE
# ══════════════════════════════════════════
def build_message(ticker: str, coin_id: str, update_num: int) -> str:
    now = datetime.now(timezone.utc).strftime("%d %b %Y  %H:%M:%S UTC")
    price, change_24h = get_price(coin_id)
    change_emoji = "📈" if change_24h >= 0 else "📉"
    change_str = f"{change_emoji} {change_24h:+.2f}% (24h)"

    lines = [
        f"📊 <b>{ticker}/USDT</b>  •  Update #{update_num}",
        f"💰 {fmt_price(price)}  {change_str}",
        f"🕐 {now}",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]

    for tf, cfg in TIMEFRAMES.items():
        df = fetch_ohlc(coin_id, cfg["days"])
        vol = fetch_volume_series(coin_id, cfg["days"])

        if df is None or len(df) < RSI_PERIOD + 5:
            lines += [f"\n⏱ <b>{cfg['label']} ({tf})</b>", "⚠️ Data unavailable"]
            continue

        if vol is not None and len(vol) >= len(df):
            df["volume"] = vol.values[:len(df)]

        rsi = calc_rsi(df["close"])
        mfi = calc_mfi(df)
        is_spike, spike_ratio = detect_volume_spike(vol)

        rsi_str = f"{rsi:.1f}" if not np.isnan(rsi) else "N/A"
        mfi_str = f"{mfi:.1f}" if not np.isnan(mfi) else "N/A"

        vol_line = ""
        if is_spike:
            vol_line = f"\n🚨 VOLUME SPIKE {spike_ratio}x avg!"

        lines += [
            f"\n⏱ <b>{cfg['label']} ({tf})</b>",
            f"RSI: <b>{rsi_str}</b>  [{rsi_bar(rsi)}]",
            f"     {rsi_label(rsi)}",
            f"MFI: <b>{mfi_str}</b>  [{rsi_bar(mfi)}]",
            f"     {mfi_label(mfi)}",
        ]
        if vol_line:
            lines.append(vol_line)

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━",
        f"⏹ /stop_{ticker.lower()} — tracking band karo",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════
#  TRACKING WORKER THREAD
# ══════════════════════════════════════════
def tracking_worker(chat_id: int, ticker: str, coin_id: str, stop_event: threading.Event):
    """Runs in background — sends update every 60 seconds"""
    log.info(f"Tracking started: {ticker} for chat {chat_id}")
    update_num = 0
    message_id = None

    while not stop_event.is_set():
        update_num += 1
        try:
            msg_text = build_message(ticker, coin_id, update_num)

            if message_id is None:
                # First message — send new
                r = requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": msg_text, "parse_mode": "HTML"},
                    timeout=10
                )
                if r.status_code == 200:
                    message_id = r.json().get("result", {}).get("message_id")
            else:
                # Edit existing message (clean, no spam)
                tg_edit(chat_id, message_id, msg_text)

        except Exception as e:
            log.error(f"Worker error ({ticker}): {e}")

        # Wait 60s, but check stop_event every second
        for _ in range(UPDATE_INTERVAL):
            if stop_event.is_set():
                break
            time.sleep(1)

    log.info(f"Tracking stopped: {ticker} for chat {chat_id}")
    tg_send(chat_id, f"⏹ <b>{ticker}</b> tracking band ho gaya.")


# ══════════════════════════════════════════
#  SESSION MANAGEMENT
# ══════════════════════════════════════════
def start_tracking(chat_id: int, ticker: str):
    try:
        coin_id, display = resolve_coin(ticker)
    except ValueError as e:
        tg_send(chat_id, f"❌ {e}")
        return

    with session_lock:
        if chat_id not in active_sessions:
            active_sessions[chat_id] = {}

        # Already tracking?
        if display in active_sessions[chat_id]:
            tg_send(chat_id, f"⚠️ <b>{display}</b> already track ho raha hai!\n/stop_{display.lower()} se band karo pehle.")
            return

        stop_event = threading.Event()
        active_sessions[chat_id][display] = stop_event

    tg_send(chat_id,
        f"✅ <b>{display}/USDT</b> tracking shuru!\n"
        f"⏱ Updates: har 1 minute\n"
        f"📊 Timeframes: 5m | 1H | 4H\n"
        f"⏹ Band karne ke liye: /stop_{display.lower()}\n"
        f"⏹ Sab band karne ke liye: /stopall\n\n"
        f"⏳ Pehla update aa raha hai..."
    )

    t = threading.Thread(
        target=tracking_worker,
        args=(chat_id, display, coin_id, stop_event),
        daemon=True
    )
    t.start()


def stop_tracking(chat_id: int, ticker: str):
    ticker = ticker.upper()
    with session_lock:
        sessions = active_sessions.get(chat_id, {})
        if ticker not in sessions:
            tg_send(chat_id, f"⚠️ <b>{ticker}</b> track nahi ho raha tha.")
            return
        sessions[ticker].set()
        del sessions[ticker]


def stop_all(chat_id: int):
    with session_lock:
        sessions = active_sessions.get(chat_id, {})
        if not sessions:
            tg_send(chat_id, "⚠️ Koi coin track nahi ho raha.")
            return
        coins = list(sessions.keys())
        for ev in sessions.values():
            ev.set()
        active_sessions[chat_id] = {}

    tg_send(chat_id, f"⏹ Sab tracking band:\n" + "\n".join(f"• {c}" for c in coins))


def list_active(chat_id: int):
    with session_lock:
        sessions = active_sessions.get(chat_id, {})
        if not sessions:
            tg_send(chat_id, "📭 Koi coin track nahi ho raha.\nKoi coin naam likho shuru karne ke liye.")
            return
        coins = list(sessions.keys())

    tg_send(chat_id,
        f"📡 <b>Active Tracking ({len(coins)} coins):</b>\n" +
        "\n".join(f"• {c} → /stop_{c.lower()}" for c in coins)
    )


# ══════════════════════════════════════════
#  WEBHOOK HANDLER
# ══════════════════════════════════════════
@app.route(f"/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        return "ok"

    msg = data.get("message") or data.get("edited_message")
    if not msg:
        return "ok"

    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    if not text:
        return "ok"

    log.info(f"Incoming [{chat_id}]: {text[:60]}")

    lower = text.lower()

    # /start /help
    if lower in ("/start", "/help"):
        tg_send(chat_id,
            "🤖 <b>Crypto RSI+MFI Tracker Bot</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "📌 <b>Kaise use karein:</b>\n\n"
            "Seedha coin symbol likho:\n"
            "<code>BTC</code>  →  Bitcoin track shuru\n"
            "<code>ETH</code>  →  Ethereum track shuru\n"
            "<code>PEPE</code> →  Pepe track shuru\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⏹ <code>/stop_btc</code> — BTC band karo\n"
            "⏹ <code>/stopall</code>  — Sab band karo\n"
            "📡 <code>/list</code>    — Active coins dekho\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⏱ Update: har 1 minute\n"
            "📊 Timeframes: 5m | 1H | 4H\n"
            "🔔 Volume Spike bhi batayega\n"
            "💹 Sab coins supported: Alt, Meme, Stable"
        )
        return "ok"

    # /stopall
    if lower == "/stopall":
        stop_all(chat_id)
        return "ok"

    # /list
    if lower == "/list":
        list_active(chat_id)
        return "ok"

    # /stop_<coin>
    if lower.startswith("/stop_"):
        coin = text[6:].upper()
        if coin:
            stop_tracking(chat_id, coin)
        return "ok"

    # Coin name (any text that looks like a ticker)
    # Strip / if present (e.g. /btc)
    raw = text.lstrip("/").strip().upper().split()[0]

    # Filter out unknown commands
    if raw.startswith("START") or raw.startswith("HELP"):
        return "ok"

    # Try to track
    start_tracking(chat_id, raw)
    return "ok"


# ══════════════════════════════════════════
#  SETUP & HEALTH ROUTES
# ══════════════════════════════════════════
@app.route("/set_webhook")
def set_webhook():
    if not BOT_TOKEN or not WEBHOOK_URL:
        return "BOT_TOKEN ya WEBHOOK_URL env variable missing!", 400
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
    r = requests.post(url, json={"url": f"{WEBHOOK_URL}/webhook"}, timeout=10)
    return jsonify(r.json())


@app.route("/delete_webhook")
def delete_webhook():
    if not BOT_TOKEN:
        return "BOT_TOKEN missing!", 400
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=10)
    return jsonify(r.json())


@app.route("/")
def home():
    active = sum(len(v) for v in active_sessions.values())
    return f"✅ Crypto RSI+MFI Bot Running | Active tracking: {active} coins"


@app.route("/health")
def health():
    active = {str(k): list(v.keys()) for k, v in active_sessions.items()}
    return jsonify({
        "status": "ok",
        "bot_configured": bool(BOT_TOKEN),
        "webhook_url": WEBHOOK_URL,
        "active_sessions": active
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"Starting bot on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
