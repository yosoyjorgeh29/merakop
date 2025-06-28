import os
import logging
import asyncio
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
)
from ta.trend import ADXIndicator, EMAIndicator
from ta.momentum import RSIIndicator
from pocketoptionapi_async import AsyncPocketOptionClient

# ── Configuración de tokens ───────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "7671720295:AAE2lS117sYptYhCmGBAXJz4PD75GK70pZc")
TWELVE_KEY_1 = os.environ.get("TWELVE_KEY_1", "f41974fed18f4abe9d5d95a633087f26")
TWELVE_KEY_2 = os.environ.get("TWELVE_KEY_2", "992d5b4012c1486e918a478d95ca8740")
TWELVE_KEY_3 = os.environ.get("TWELVE_KEY_3", "df99225aa1dd41958ff3414cf351b8b8")
TWELVE_KEY_4 = os.environ.get("TWELVE_KEY_4", "84104351426c4daea9fd70f598f7490c")
TWELVE_KEY_5 = os.environ.get("TWELVE_KEY_5", "1ea81844682e45a6b29fa75c8c3adc38")
TWELVE_KEY_6 = os.environ.get("TWELVE_KEY_6", "fae654949bf74f609361d4c05e028c22")

# ── Cliente PocketOption ASÍNCRONO ─────────────────────────────────────────────
PO_SS_ID = "AvqdN1xRBcAKMLBf7"  # tu SSID real o exportado en PO_SS_ID
PO_CLIENT = AsyncPocketOptionClient(PO_SS_ID)
asyncio.get_event_loop().run_until_complete(PO_CLIENT.connect())

# ── Estados de la conversación ─────────────────────────────────────────────────
CHOOSE_MARKET, CHOOSE_PAIR, WAIT_SIGNAL, WAIT_RESULT = range(4)

# ── Pares Forex reales y sus banderas ──────────────────────────────────────────
FOREX_PAIRS = {
    "AUD/CAD": "🇦🇺/🇨🇦", "AUD/JPY": "🇦🇺/🇯🇵", "AUD/USD": "🇦🇺/🇺🇸",
    "AUD/CHF": "🇦🇺/🇨🇭", "CAD/CHF": "🇨🇦/🇨🇭", "CAD/JPY": "🇨🇦/🇯🇵",
    "CHF/JPY": "🇨🇭/🇯🇵", "EUR/AUD": "🇪🇺/🇦🇺", "EUR/CAD": "🇪🇺/🇨🇦",
    "EUR/CHF": "🇪🇺/🇨🇭", "EUR/GBP": "🇪🇺/🇬🇧", "USD/CAD": "🇺🇸/🇨🇦",
    "USD/CHF": "🇺🇸/🇨🇭", "USD/JPY": "🇺🇸/🇯🇵", "GBP/CAD": "🇬🇧/🇨🇦",
    "GBP/CHF": "🇬🇧/🇨🇭",
}

# ── Pares OTC (mismos símbolos + “-OTC”) ───────────────────────────────────────
OTC_PAIRS = {f"{p}-OTC": flag for p, flag in FOREX_PAIRS.items()}

# ── Asignación de API keys TwelveData ─────────────────────────────────────────
PAIR_TO_KEY = {
    **{p: TWELVE_KEY_1 for p in ["AUD/CAD","AUD/JPY","AUD/USD"]},
    **{p: TWELVE_KEY_2 for p in ["AUD/CHF","CAD/CHF","CAD/JPY"]},
    **{p: TWELVE_KEY_3 for p in ["CHF/JPY","EUR/AUD","EUR/CAD"]},
    **{p: TWELVE_KEY_4 for p in ["EUR/CHF","EUR/GBP","USD/CAD"]},
    **{p: TWELVE_KEY_5 for p in ["USD/CHF","USD/JPY","GBP/CAD"]},
    **{"GBP/CHF": TWELVE_KEY_6},
}

# ── Descarga velas TwelveData síncrona ─────────────────────────────────────────
def fetch_candles_sync(pair: str, interval="5min", outputsize=30) -> pd.DataFrame:
    apikey = PAIR_TO_KEY[pair]
    resp = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": pair,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": apikey,
            "format": "JSON",
        }, timeout=10
    )
    resp.raise_for_status()
    df = pd.DataFrame(resp.json().get("values", []))[::-1]
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.set_index("datetime", inplace=True)
    for c in ("open","high","low","close"):
        df[c] = pd.to_numeric(df[c])
    return df

# ── Descarga velas PocketOption asíncrona o con hilo ─────────────────────────────
async def fetch_candles_async(pair: str, interval="5min", outputsize=30) -> pd.DataFrame:
    if pair.endswith("-OTC"):
        symbol = pair.replace("/", "").replace("-OTC", "")
        data = await PO_CLIENT.get_candles(symbol=symbol, period="5m", count=outputsize)
        df = pd.DataFrame(data)
        df['datetime'] = pd.to_datetime(df['from'], unit='ms')
        df.set_index('datetime', inplace=True)
        df.rename(columns={'max':'high','min':'low'}, inplace=True)
        for c in ('open','high','low','close'):
            df[c] = pd.to_numeric(df[c])
        return df
    # Forex: delegar síncrono en hilo
    return await asyncio.to_thread(fetch_candles_sync, pair, interval, outputsize)

# ── Cálculo ATR ────────────────────────────────────────────────────────────────
def compute_atr(df: pd.DataFrame, length=14) -> float:
    tr1 = df['high'] - df['low']
    tr2 = (df['high'] - df['close'].shift()).abs()
    tr3 = (df['low']  - df['close'].shift()).abs()
    tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return float(tr.rolling(length).mean().iloc[-1])

# ── Lógica principal de retesteo ───────────────────────────────────────────────
async def check_retest(df5: pd.DataFrame, pair: str, entry_time: datetime) -> tuple[str,float] | None:
    adx = ADXIndicator(df5['high'], df5['low'], df5['close'], window=14).adx().iloc[-1]
    if adx < 25:
        return None
    atr = compute_atr(df5,14)
    if atr < (df5['high'] - df5['low']).mean() * 0.3:
        return None
    y = df5['close'].values; x = np.arange(len(y))
    m,b = np.polyfit(x, y, 1)
    last = df5.iloc[-1]
    trend_price = m*(len(y)-1) + b
    gap = atr * 0.2
    if m>0 and last['low'] > trend_price + gap: return None
    if m<0 and last['high'] < trend_price - gap:  return None
    sig = 'CALL' if m>0 else 'PUT'
    score = adx * (gap + abs(last['close'] - trend_price))
    rsi = RSIIndicator(df5['close'], window=14).rsi().iloc[-1]
    if sig=='CALL' and rsi < 50: return None
    if sig=='PUT'  and rsi > 50: return None
    df1h = await fetch_candles_async(pair, interval='1h', outputsize=50)
    ema50  = EMAIndicator(df1h['close'], window=50).ema_indicator().iloc[-1]
    ema200 = EMAIndicator(df1h['close'], window=200).ema_indicator().iloc[-1]
    if sig=='CALL' and ema50 <= ema200: return None
    if sig=='PUT'  and ema50 >= ema200: return None
    return sig, score

# ── Handlers de Telegram ──────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💹 Mercado Real", callback_data="MARKET_REAL")],
        [InlineKeyboardButton("📈 Mercado OTC",   callback_data="MARKET_OTC")],
    ]
    await update.message.reply_text("⭐️ Elige el mercado:", reply_markup=InlineKeyboardMarkup(kb))
    return CHOOSE_MARKET

async def choose_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    mtype = q.data.split('_')[1]
    context.user_data['market'] = mtype
    kb = [[InlineKeyboardButton("🤖 Automático", callback_data="AUTO")]]
    mapping = FOREX_PAIRS if mtype=='REAL' else OTC_PAIRS
    row = []
    for pair,flag in mapping.items():
        row.append(InlineKeyboardButton(f"{flag} {pair}", callback_data=pair))
        if len(row)==2: kb.append(row); row=[]
    if row: kb.append(row)
    await q.edit_message_text("⭐️ Elige par o modo Automático:", reply_markup=InlineKeyboardMarkup(kb))
    return CHOOSE_PAIR

async def choose_pair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sel = q.data
    now = datetime.utcnow()
    m5 = (now.minute // 5 + 1) * 5
    entry = now.replace(minute=m5%60, second=0, microsecond=0)
    if m5 >= 60: entry += timedelta(hours=1)
    if sel=='AUTO':
        intro = await q.edit_message_text("🤖 Automático: escaneando…")
        pairs = list(FOREX_PAIRS) + list(OTC_PAIRS)
    else:
        intro = await q.edit_message_text(
            f"🎯 Has elegido *{sel}*.",
            parse_mode="Markdown"
        )
        await context.bot.send_message(q.message.chat_id,
            f"⏰ Entrada (cierre M5): {(entry - timedelta(minutes=1)).strftime('%H:%M')} UTC"
        )
        pairs = [sel]
    wait = (entry - timedelta(minutes=1) - datetime.utcnow()).total_seconds()
    context.job_queue.run_once(send_signal, when=max(wait,0), data={
        'chat_id': q.message.chat_id,
        'pairs': pairs,
        'intro_id': intro.message_id,
        'entry_time': entry
    })
    return WAIT_SIGNAL

async def send_signal(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    chat, pairs, entry = d['chat_id'], d['pairs'], d['entry_time']
    try: await context.bot.delete_message(chat, d['intro_id'])
    except: pass
    best=None
    for p in pairs:
        df5 = await fetch_candles_async(p, '5min', 30)
        out = await check_retest(df5, p, entry)
        if not out: continue
        sig,score = out
        if best is None or score>best['score']:
            best = {'pair':p, 'signal':sig, 'score':score}
    if not best:
        await context.bot.send_message(chat, '⚠️ No señal clara.')
        return WAIT_SIGNAL
    emoji = '🟢' if best['signal']=='CALL' else '🔴'
    text = (
        f"🤖 Señal generada:\n"
        f"🌐 Activo: {best['pair']}\n"
        f"📈 Dirección: {emoji} {best['signal']}\n"
        f"⏰ Entrada (cierre M5): {entry.strftime('%H:%M')} UTC\n"
        "🎯 ¡Martingale permitido!"
    )
    await context.bot.send_message(chat, text)
    delay = (entry + timedelta(minutes=5) - datetime.utcnow()).total_seconds()
    context.job_queue.run_once(check_result, when=max(delay,0), data={
        'chat_id': chat,
        'pair': best['pair'],
        'signal': best['signal'],
        'entry_time': entry
    })
    return WAIT_RESULT

async def check_result(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    chat, pair, sig, entry = d['chat_id'], d['pair'], d['signal'], d['entry_time']
    df5 = await fetch_candles_async(pair, '5min', 30)
    try: candle = df5.loc[entry]
    except:
        idx = df5.index.get_indexer([entry], method='nearest')[0]
        candle = df5.iloc[idx]
    won = (sig=='CALL' and candle['close']>candle['open']) or (sig=='PUT' and candle['close']<candle['open'])
    if won:
        await context.bot.send_message(chat, '✅ GANADA 🟢')
        await context.bot.send_message(chat, '🔄 Para nuevo análisis, /start')
        return ConversationHandler.END
    delay2 = (entry + timedelta(minutes=10) - datetime.utcnow()).total_seconds()
    context.job_queue.run_once(check_martingale, when=max(delay2,0), data=d)
    return WAIT_RESULT

async def check_martingale(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    chat, pair, sig, entry = d['chat_id'], d['pair'], d['signal'], d['entry_time']
    df5 = await fetch_candles_async(pair, '5min', 30)
    t10 = entry + timedelta(minutes=10)
    try: candle = df5.loc[t10]
    except:
        idx = df5.index.get_indexer([t10], method='nearest')[0]
        candle = df5.iloc[idx]
    won2 = (sig=='CALL' and candle['close']>candle['open']) or (sig=='PUT' and candle['close']<candle['open'])
    text = '✅ GANADA 🟢 (Martingale)' if won2 else '❌ PERDIDA 🔴 (Martingale)'
    await context.bot.send_message(chat, text)
    await context.bot.send_message(chat, '🔄 Para nuevo análisis, /start')
    return ConversationHandler.END


def main():
    logging.basicConfig(format="%(asctime)s %(levelname)s - %(message)s", level=logging.INFO)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            CHOOSE_MARKET: [CallbackQueryHandler(choose_market, pattern='^MARKET_')],
            CHOOSE_PAIR:   [CallbackQueryHandler(choose_pair)],
            WAIT_SIGNAL:   [],
            WAIT_RESULT:   [],
        },
        fallbacks=[CommandHandler('start', start)],
        per_chat=True,
    )
    app.add_handler(conv)
    app.run_polling()

if __name__ == '__main__':
    main()
