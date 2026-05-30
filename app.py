"""
Crypto RSI + MFI Telegram Bot — POLLING MODE
- Koi webhook nahi, koi URL nahi
- Sirf TELEGRAM_BOT_TOKEN chahiye
- Bot khud Telegram se messages check karta hai
"""

import os, time, threading, logging
from datetime import datetime, timezone
import requests
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
RSI_PERIOD     = 14
MFI_PERIOD     = 14
VOLUME_SPIKE_X = 2.0
UPDATE_INTERVAL = 60

TIMEFRAMES = {
    "5m": {"days": 1,  "label": "5 Min"},
    "1H": {"days": 30, "label": "1 Hour"},
    "4H": {"days": 90, "label": "4 Hour"},
}

active_sessions: dict = {}
session_lock = threading.Lock()
coin_cache: dict = {}

KNOWN_COINS = {
    "BTC":"bitcoin","ETH":"ethereum","SOL":"solana","BNB":"binancecoin",
    "XRP":"ripple","ADA":"cardano","DOGE":"dogecoin","AVAX":"avalanche-2",
    "DOT":"polkadot","MATIC":"matic-network","LINK":"chainlink","UNI":"uniswap",
    "LTC":"litecoin","ATOM":"cosmos","NEAR":"near","APT":"aptos","ARB":"arbitrum",
    "OP":"optimism","SUI":"sui","PEPE":"pepe","WIF":"dogwifcoin","SHIB":"shiba-inu",
    "TON":"the-open-network","TRX":"tron","FIL":"filecoin","AAVE":"aave",
    "INJ":"injective-protocol","SEI":"sei-network","HBAR":"hedera-hashgraph",
    "VET":"vechain","BONK":"bonk","WLD":"worldcoin-wld","RENDER":"render-token",
    "JUP":"jupiter-exchange-solana","TIA":"celestia","BLUR":"blur","GMX":"gmx",
    "GRT":"the-graph","LDO":"lido-dao","RUNE":"thorchain","CRV":"curve-dao-token",
    "MKR":"maker","ENS":"ethereum-name-service","IMX":"immutable-x","MANA":"decentraland",
    "SAND":"the-sandbox","AXS":"axie-infinity","GALA":"gala","CHZ":"chiliz",
    "FTM":"fantom","KAVA":"kava","ZEC":"zcash","XMR":"monero","ETC":"ethereum-classic",
    "BCH":"bitcoin-cash","XTZ":"tezos","ALGO":"algorand","BAT":"basic-attention-token",
    "USDT":"tether","USDC":"usd-coin","DAI":"dai","FRAX":"frax",
    "PYTH":"pyth-network","STRK":"starknet","ORDI":"ordinals",
}

def tg_send(chat_id: int, text: str) -> int | None:
    try:
        r = requests.post(f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10)
        if r.status_code == 200:
            return r.json().get("result", {}).get("message_id")
    except Exception as e:
        log.error(f"tg_send: {e}")
    return None

def tg_edit(chat_id: int, msg_id: int, text: str):
    try:
        requests.post(f"{API}/editMessageText",
            json={"chat_id": chat_id, "message_id": msg_id,
                  "text": text, "parse_mode": "HTML"},
            timeout=10)
    except Exception as e:
        log.error(f"tg_edit: {e}")

def resolve_coin(ticker: str) -> tuple:
    t = ticker.upper().strip()
    if t in KNOWN_COINS:
        return KNOWN_COINS[t], t
    if t in coin_cache:
        return coin_cache[t], t
    try:
        r = requests.get(f"{COINGECKO_BASE}/search", params={"query": t}, timeout=10)
        coins = r.json().get("coins", [])
        for c in coins:
            if c["symbol"].upper() == t:
                coin_cache[t] = c["id"]
                return c["id"], c["symbol"].upper()
        if coins:
            c = coins[0]
            sym = c["symbol"].upper()
            coin_cache[sym] = c["id"]
            return c["id"], sym
    except Exception as e:
        log.error(f"resolve: {e}")
    raise ValueError(f"'{ticker}' nahi mila. Sahi symbol daalo (BTC, PEPE, WIF...)")

def fetch_ohlc(coin_id: str, days: int):
    try:
        r = requests.get(f"{COINGECKO_BASE}/coins/{coin_id}/ohlc",
            params={"vs_currency": "usd", "days": str(days)}, timeout=15)
        if r.status_code == 429:
            time.sleep(61)
            return None
        if r.status_code != 200:
            return None
        data = r.json()
        if not data:
            return None
        df = pd.DataFrame(data, columns=["ts","open","high","low","close"])
        for c in ["open","high","low","close"]:
            df[c] = df[c].astype(float)
        df["volume"] = 1.0
        return df
    except Exception:
        return None

def fetch_vol(coin_id: str, days: int):
    try:
        r = requests.get(f"{COINGECKO_BASE}/coins/{coin_id}/market_chart",
            params={"vs_currency": "usd", "days": str(days)}, timeout=15)
        if r.status_code != 200:
            return None
        vols = r.json().get("total_volumes", [])
        return pd.DataFrame(vols, columns=["ts","volume"])["volume"] if vols else None
    except Exception:
        return None

def get_price(coin_id: str):
    try:
        r = requests.get(f"{COINGECKO_BASE}/simple/price",
            params={"ids": coin_id, "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=8)
        d = r.json().get(coin_id, {})
        return float(d.get("usd", 0)), float(d.get("usd_24h_change", 0))
    except Exception:
        return 0.0, 0.0

def calc_rsi(close):
    if len(close) < RSI_PERIOD + 1: return float("nan")
    d = close.diff()
    ag = d.clip(lower=0).ewm(alpha=1/RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    al = (-d).clip(lower=0).ewm(alpha=1/RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    val = float((100 - 100/(1 + ag/al.replace(0, np.nan))).iloc[-1])
    return round(val, 2) if not np.isnan(val) else float("nan")

def calc_mfi(df):
    if len(df) < MFI_PERIOD + 1: return float("nan")
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    rmf = tp * df["volume"]
    prev = tp.shift(1)
    pos = rmf.where(tp > prev, 0).rolling(MFI_PERIOD).sum()
    neg = rmf.where(tp < prev, 0).rolling(MFI_PERIOD).sum()
    val = float((100 - 100/(1 + pos/neg.replace(0, np.nan))).iloc[-1])
    return round(val, 2) if not np.isnan(val) else float("nan")

def vol_spike(vol_series):
    if vol_series is None or len(vol_series) < 21: return False, 0.0
    avg = float(vol_series.iloc[-21:-1].mean())
    cur = float(vol_series.iloc[-1])
    if avg == 0: return False, 0.0
    ratio = cur / avg
    return ratio >= VOLUME_SPIKE_X, round(ratio, 2)

def bar(v, w=12):
    if np.isnan(v): return "░" * w
    f = max(0, min(w, int(v/100*w)))
    return "█"*f + "░"*(w-f)

def rsi_label(v):
    if np.isnan(v): return "❓ N/A"
    if v >= 70: return "🔴 OVERBOUGHT"
    if v <= 30: return "🟢 OVERSOLD"
    if v >= 60: return "🟡 Bullish"
    if v <= 40: return "🟠 Bearish"
    return "⚪ Neutral"

def mfi_label(v):
    if np.isnan(v): return "❓ N/A"
    if v >= 80: return "🔴 OVERBOUGHT"
    if v <= 20: return "🟢 OVERSOLD"
    if v >= 65: return "🟡 Buy Pressure"
    if v <= 35: return "🟠 Sell Pressure"
    return "⚪ Neutral"

def fmt_price(p):
    if p == 0:    return "N/A"
    if p >= 1000: return f"${p:,.2f}"
    if p >= 1:    return f"${p:.4f}"
    if p >= 0.01: return f"${p:.6f}"
    return               f"${p:.10f}"

def build_message(ticker: str, coin_id: str, update_num: int) -> str:
    now = datetime.now(timezone.utc).strftime("%d %b  %H:%M:%S UTC")
    price, chg = get_price(coin_id)
    chg_str = f"{'📈' if chg >= 0 else '📉'} {chg:+.2f}%"
    lines = [
        f"📊 <b>{ticker}/USDT</b>  #{update_num}",
        f"💰 <b>{fmt_price(price)}</b>  {chg_str} (24h)",
        f"🕐 {now}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    for tf, cfg in TIMEFRAMES.items():
        df  = fetch_ohlc(coin_id, cfg["days"])
        vol = fetch_vol(coin_id, cfg["days"])
        if df is None or len(df) < RSI_PERIOD + 5:
            lines += [f"\n⏱ <b>{cfg['label']} ({tf})</b>", "⚠️ Data unavailable"]
            continue
        if vol is not None and len(vol) >= len(df):
            df["volume"] = vol.values[:len(df)]
        rsi = calc_rsi(df["close"])
        mfi = calc_mfi(df)
        spike, ratio = vol_spike(vol)
        rsi_s = f"{rsi:.1f}" if not np.isnan(rsi) else "N/A"
        mfi_s = f"{mfi:.1f}" if not np.isnan(mfi) else "N/A"
        lines += [
            f"\n⏱ <b>{cfg['label']} ({tf})</b>",
            f"RSI: <b>{rsi_s}</b>  [{bar(rsi)}]  {rsi_label(rsi)}",
            f"MFI: <b>{mfi_s}</b>  [{bar(mfi)}]  {mfi_label(mfi)}",
        ]
        if spike:
            lines.append(f"🚨 VOLUME SPIKE <b>{ratio}x</b> avg!")
    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏹ /stop_{ticker.lower()}  |  /stopall  |  /list",
    ]
    return "\n".join(lines)

def worker(chat_id: int, ticker: str, coin_id: str, stop_event: threading.Event):
    log.info(f"START {ticker} for {chat_id}")
    update_num = 0
    message_id = None
    while not stop_event.is_set():
        update_num += 1
        try:
            text = build_message(ticker, coin_id, update_num)
            if message_id is None:
                message_id = tg_send(chat_id, text)
            else:
                tg_edit(chat_id, message_id, text)
        except Exception as e:
            log.error(f"Worker ({ticker}): {e}")
        for _ in range(UPDATE_INTERVAL):
            if stop_event.is_set(): break
            time.sleep(1)
    log.info(f"STOP {ticker} for {chat_id}")
    tg_send(chat_id, f"⏹ <b>{ticker}</b> tracking band ho gaya.")

def start_tracking(chat_id: int, ticker: str):
    try:
        coin_id, display = resolve_coin(ticker)
    except ValueError as e:
        tg_send(chat_id, f"❌ {e}")
        return
    with session_lock:
        if chat_id not in active_sessions:
            active_sessions[chat_id] = {}
        if display in active_sessions[chat_id]:
            tg_send(chat_id, f"⚠️ <b>{display}</b> already track ho raha hai!\n/stop_{display.lower()} se pehle band karo.")
            return
        ev = threading.Event()
        active_sessions[chat_id][display] = ev
    tg_send(chat_id,
        f"✅ <b>{display}/USDT</b> tracking shuru!\n"
        f"⏱ Har 1 minute update\n"
        f"📊 5m | 1H | 4H\n"
        f"⏹ Band karo: /stop_{display.lower()}\n"
        f"⏳ Pehla update aa raha hai..."
    )
    threading.Thread(target=worker, args=(chat_id, display, coin_id, ev), daemon=True).start()

def stop_one(chat_id: int, ticker: str):
    t = ticker.upper()
    with session_lock:
        s = active_sessions.get(chat_id, {})
        if t not in s:
            tg_send(chat_id, f"⚠️ <b>{t}</b> track nahi ho raha tha.")
            return
        s[t].set()
        del s[t]

def stop_all(chat_id: int):
    with session_lock:
        s = active_sessions.get(chat_id, {})
        if not s:
            tg_send(chat_id, "⚠️ Koi coin track nahi ho raha.")
            return
        coins = list(s.keys())
        for ev in s.values(): ev.set()
        active_sessions[chat_id] = {}
    tg_send(chat_id, "⏹ Sab tracking band:\n" + "\n".join(f"• {c}" for c in coins))

def list_active(chat_id: int):
    with session_lock:
        s = active_sessions.get(chat_id, {})
        coins = list(s.keys())
    if not coins:
        tg_send(chat_id, "📭 Koi coin track nahi ho raha.\nKoi bhi coin symbol likho shuru karne ke liye.")
        return
    tg_send(chat_id,
        f"📡 <b>Active ({len(coins)} coins):</b>\n" +
        "\n".join(f"• {c}  →  /stop_{c.lower()}" for c in coins)
    )

IGNORE_CMDS = {"start", "help", "stop", "list", "stopall"}

def handle_message(msg: dict):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    if not text:
        return
    log.info(f"[{chat_id}] {text[:80]}")
    low = text.lower().strip()
    if low in ("/start", "/help", "start", "help"):
        tg_send(chat_id,
            "🤖 <b>Crypto RSI+MFI Bot</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "📌 <b>Kaise use karein:</b>\n\n"
            "Bas coin ka naam likho:\n"
            "<code>BTC</code>  →  Bitcoin\n"
            "<code>ETH</code>  →  Ethereum\n"
            "<code>PEPE</code> →  Pepe\n"
            "<code>WIF</code>  →  Dogwifhat\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⏹ <code>/stop_btc</code>  — BTC band karo\n"
            "⏹ <code>/stopall</code>   — Sab band karo\n"
            "📡 <code>/list</code>     — Active coins\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⏱ Har <b>1 minute</b> update\n"
            "📊 <b>5m | 1H | 4H</b>\n"
            "🚨 Volume Spike detection ON"
        )
        return
    if low == "/stopall":
        stop_all(chat_id); return
    if low == "/list":
        list_active(chat_id); return
    if low.startswith("/stop_"):
        coin = text[6:].strip().upper()
        if coin: stop_one(chat_id, coin)
        return
    raw = text.lstrip("/").strip().upper().split()[0]
    if raw.lower() in IGNORE_CMDS:
        return
    if raw.isalpha() and 2 <= len(raw) <= 12:
        start_tracking(chat_id, raw)
    else:
        tg_send(chat_id, "❓ Coin symbol likho: <code>BTC</code>, <code>ETH</code>, <code>PEPE</code>")

def polling_loop():
    log.info("Polling loop shuru...")
    offset = None
    try:
        requests.post(f"{API}/deleteWebhook", timeout=10)
        log.info("Webhook deleted — polling active")
    except Exception:
        pass
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            r = requests.get(f"{API}/getUpdates", params=params, timeout=35)
            if r.status_code != 200:
                log.warning(f"getUpdates failed: {r.status_code}")
                time.sleep(5)
                continue
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or update.get("edited_message")
                if msg:
                    try:
                        handle_message(msg)
                    except Exception as e:
                        log.error(f"handle_message: {e}")
        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            log.error(f"Polling: {e}")
            time.sleep(5)

if __name__ == "__main__":
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN set nahi hai!")
        exit(1)
    log.info("Bot start — POLLING MODE")
    polling_loop()
