import os
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

import ccxt
import pandas as pd
import numpy as np
import requests
import time
import json
from datetime import datetime
from pathlib import Path
import warnings
warnings.filterwarnings('ignore')

# ================= Configuration =================
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN') or os.environ.get('BOT_TOKEN')
CHANNEL_ID = os.environ.get('CHANNEL_ID')

TIMEFRAME_ENTRY = '15m'
LEVERAGE = 10

# ✅ وقف خسارة ثابت 1.5%
SL_PERCENT = 1.5

# ✅ هدفان فقط
TP1_PERCENT = 0.85
TP2_PERCENT = 2.0

# ✅ فلتر حجم جسم شمعة Heikin Ashi (أقل من 0.2% = لا تداول)
MIN_HA_BODY = 0.2

# ✅ نافذة التداول: أول 18 ساعة من اليوم
TRADING_WINDOW_HOURS = 9

# Quality Filters
MIN_ATR_PERCENT = 0.03
VOLUME_LOOKBACK = 20
COOLDOWN_HOURS = 3
COOLDOWN_FILE = Path('cooldown.json')

# ✅ قائمة بيضاء بأزواج Futures الموثوقة فقط
WHITELIST = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
    "LINK/USDT", "AVAX/USDT", "ADA/USDT", "DOT/USDT", "LTC/USDT",
    "TRX/USDT", "UNI/USDT", "ATOM/USDT", "XLM/USDT", "FIL/USDT",
    "NEAR/USDT", "SUI/USDT", "SEI/USDT", "INJ/USDT", "APT/USDT",
    "AAVE/USDT", "FET/USDT", "RENDER/USDT", "TIA/USDT", "ONDO/USDT",
    "ARB/USDT", "OP/USDT"
]

STABLECOINS = ['USDC/USDT', 'TUSD/USDT', 'DAI/USDT', 'FDUSD/USDT', 'USDP/USDT', 'PYUSD/USDT']
BLACKLIST = ['WXT/USDT', 'ANTFUN/USDT', 'UPC/USDT', 'RAIN/USDT', 'USD1/USDT', 'USDE/USDT', 'BEAT/USDT', 'AKE/USDT', 'MY/USDT', 'MBG/USDT', 'AIX/USDT', 'XPLK/USDT', 'ZAY/USDT', '9BIT/USDT', 'CYS/USDT', 'GOLD/USDT']


def _fmt(price):
    if price >= 1000:   return f"{price:,.2f}"
    elif price >= 1:    return f"{price:,.4f}"
    else:              return f"{price:,.6f}"


def load_cooldown():
    if COOLDOWN_FILE.exists():
        try:
            with open(COOLDOWN_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cooldown(data):
    try:
        with open(COOLDOWN_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save cooldown: {e}")


def is_on_cooldown(symbol, cooldown_data):
    if symbol not in cooldown_data:
        return False
    try:
        last_time = datetime.fromisoformat(cooldown_data[symbol])
        elapsed = (datetime.now() - last_time).total_seconds() / 3600
        return elapsed < COOLDOWN_HOURS
    except Exception:
        return False


def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHANNEL_ID:
        print("Missing TELEGRAM_TOKEN or CHANNEL_ID")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': CHANNEL_ID,
        'text': message,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True
    }
    try:
        r = requests.post(url, json=payload, timeout=30)
        if not r.json().get('ok'):
            print(f"Telegram error: {r.json().get('description')}")
    except Exception as e:
        print(f"Network error sending Telegram: {e}")


def get_mexc_data(symbol, timeframe, limit=150):
    exchange = ccxt.mexc({'enableRateLimit': True})
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    return df


def calculate_heikin_ashi(df):
    """يحسب شموع Heikin Ashi من الشموع العادية"""
    ha_df = df.copy()
    
    # Heikin Ashi Close = (Open + High + Low + Close) / 4
    ha_df['ha_close'] = (df['open'] + df['high'] + df['low'] + df['close']) / 4
    
    # Heikin Ashi Open = (Previous HA Open + Previous HA Close) / 2
    ha_df['ha_open'] = 0.0
    ha_df.iloc[0, ha_df.columns.get_loc('ha_open')] = (df.iloc[0]['open'] + df.iloc[0]['close']) / 2
    
    for i in range(1, len(ha_df)):
        ha_df.iloc[i, ha_df.columns.get_loc('ha_open')] = (ha_df.iloc[i-1]['ha_open'] + ha_df.iloc[i-1]['ha_close']) / 2
    
    # Heikin Ashi High = Max(High, HA Open, HA Close)
    ha_df['ha_high'] = ha_df[['high', 'ha_open', 'ha_close']].max(axis=1)
    
    # Heikin Ashi Low = Min(Low, HA Open, HA Close)
    ha_df['ha_low'] = ha_df[['low', 'ha_open', 'ha_close']].min(axis=1)
    
    return ha_df


def get_top_mexc_coins(limit=50):
    print(f"Fetching top {limit} coins by volume from MEXC...")
    exchange = ccxt.mexc({'enableRateLimit': True})
    try:
        tickers = exchange.fetch_tickers()
        usdt_pairs = []
        for symbol, ticker in tickers.items():
            if symbol in WHITELIST:
                vol = ticker.get('quoteVolume') or 0
                if vol > 200000:
                    usdt_pairs.append({'symbol': symbol, 'volume': vol})
        usdt_pairs.sort(key=lambda x: x['volume'], reverse=True)
        top = [p['symbol'] for p in usdt_pairs[:limit]]
        print(f"Top coins: {top[:5]} ... ({len(top)} total)")
        return top
    except Exception as e:
        print(f"Error fetching coins: {e}")
        return []


def calculate_atr(df, period=14):
    high, low, close = df['high'], df['low'], df['close']
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def in_trading_window():
    """أول 18 ساعة من اليوم (00:00 - 18:00 UTC)"""
    now_utc = datetime.utcnow()
    return now_utc.hour < TRADING_WINDOW_HOURS


def get_ha_bias(symbol):
    """
    يحدد الاتجاه من آخر شمعة Heikin Ashi مغلقة:
    - شمعة خضراء (HA Close > HA Open) → LONG فقط
    - شمعة حمراء (HA Close < HA Open) → SHORT فقط
    - جسم أصغر من 0.2% → لا تداول (Doji)
    """
    try:
        df = get_mexc_data(symbol, '15m', limit=100)
        if len(df) < 5:
            return None
        
        # تحويل إلى Heikin Ashi
        ha_df = calculate_heikin_ashi(df)
        
        # آخر شمعتين HA مغلفتين
        last_ha = ha_df.iloc[-2]
        prev_ha = ha_df.iloc[-3]
        
        ha_open = float(last_ha['ha_open'])
        ha_close = float(last_ha['ha_close'])
        body_pct = abs(ha_close - ha_open) / ha_open * 100
        
        # فلتر حجم الجسم
        if body_pct < MIN_HA_BODY:
            print(f"  {symbol}: HA body too small ({body_pct:.3f}%) — Doji, skip")
            return None
        
        # تحديد اللون
        direction = "LONG" if ha_close > ha_open else "SHORT"
        
        # فلتر إضافي: تأكد من أن الشمعة السابقة في نفس الاتجاه (تأكيد الاتجاه)
        prev_direction = "LONG" if float(prev_ha['ha_close']) > float(prev_ha['ha_open']) else "SHORT"
        
        if direction != prev_direction:
            print(f"  {symbol}: HA direction changed — not confirmed, skip")
            return None
        
        return direction
    except Exception as e:
        print(f"  {symbol}: HA bias error - {e}")
        return None


def analyze_symbol(symbol, direction):
    try:
        df_15m = get_mexc_data(symbol, TIMEFRAME_ENTRY, limit=100)
        if len(df_15m) < 50:
            return None
        
        
        # تحويل إلى Heikin Ashi
        ha_df = calculate_heikin_ashi(df_15m)
        
        # === Indicators on 15m (using HA candles) ===
        ha_df['ema9'] = ha_df['ha_close'].ewm(span=9, adjust=False).mean()
        ha_df['ema21'] = ha_df['ha_close'].ewm(span=21, adjust=False).mean()
        
        # RSI
        delta = ha_df['ha_close'].diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.rolling(window=14).mean()
        avg_loss = loss.rolling(window=14).mean()
        rs = avg_gain / avg_loss
        ha_df['rsi'] = 100 - (100 / (1 + rs))
        
        # MACD Histogram
        ema12 = ha_df['ha_close'].ewm(span=12, adjust=False).mean()
        ema26 = ha_df['ha_close'].ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        ha_df['macd_hist'] = macd_line - signal_line
        
        # ATR & Volume
        ha_df['atr'] = calculate_atr(ha_df, 14)
        ha_df['atr_pct'] = (ha_df['atr'] / ha_df['ha_close']) * 100
        ha_df['vol_sma'] = ha_df['volume'].rolling(window=VOLUME_LOOKBACK).mean()
        
        # ✅ آخر شمعة HA *مغلقة*
        i = -2
        ema9 = ha_df['ema9'].iloc[i]
        ema21 = ha_df['ema21'].iloc[i]
        rsi = ha_df['rsi'].iloc[i]
        rsi_prev = ha_df['rsi'].iloc[i-1]
        macd_hist = ha_df['macd_hist'].iloc[i]
        macd_hist_prev = ha_df['macd_hist'].iloc[i-1]
        atr = ha_df['atr'].iloc[i]
        atr_pct = ha_df['atr_pct'].iloc[i]
        vol_now = ha_df['volume'].iloc[i]
        vol_avg = ha_df['vol_sma'].iloc[i]
        
        ha_open = float(ha_df['ha_open'].iloc[i])
        ha_close = float(ha_df['ha_close'].iloc[i])
        entry = float(ha_df['close'].iloc[i])  # سعر الدخول من الشموع العادية
        
        # === Quality Filters ===
        if pd.isna(atr_pct) or atr_pct < MIN_ATR_PERCENT:
            print(f"  {symbol}: ATR too low ({atr_pct:.3f}%)")
            return None
        
        if pd.isna(vol_avg) or vol_now < vol_avg * 0.3:
            print(f"  {symbol}: Volume too low")
            return None
        
        if pd.isna(rsi) or pd.isna(macd_hist):
            print(f"  {symbol}: Indicators not ready")
            return None
        
        # === دخول الارتداد من EMA 21 باتجاه شمعة HA ===
        confidence = 65
        
        if direction == "LONG":
            ema_aligned = ema9 > ema21
            rsi_ok = 50 < rsi <= 75
            macd_ok = macd_hist > 0 or macd_hist > macd_hist_prev
            
            if not (ema_aligned and rsi_ok and macd_ok):
                print(f"  {symbol}: No LONG setup (EMA9/21: {ema9:.4f}/{ema21:.4f}, RSI: {rsi:.1f}, MACD: {macd_hist:.5f})")
                return None
            
            if rsi_prev <= 50 < rsi: confidence += 10
            if macd_hist > 0: confidence += 5
            if vol_now > vol_avg * 1.2: confidence += 5
            
            sl = entry * (1 - SL_PERCENT / 100)
            tp1 = entry * (1 + TP1_PERCENT / 100)
            tp2 = entry * (1 + TP2_PERCENT / 100)
        
        else:  # SHORT
            ema_aligned = ema9 < ema21
            rsi_ok = 25 <= rsi < 50
            macd_ok = macd_hist < 0 or macd_hist < macd_hist_prev
            
            if not (ema_aligned and rsi_ok and macd_ok):
                print(f"  {symbol}: No SHORT setup (EMA9/21: {ema9:.4f}/{ema21:.4f}, RSI: {rsi:.1f}, MACD: {macd_hist:.5f})")
                return None
            
            if rsi_prev >= 50 > rsi: confidence += 10
            if macd_hist < 0: confidence += 5
            if vol_now > vol_avg * 1.2: confidence += 5
            
            sl = entry * (1 + SL_PERCENT / 100)
            tp1 = entry * (1 - TP1_PERCENT / 100)
            tp2 = entry * (1 - TP2_PERCENT / 100)
        
        confidence = min(95, confidence)
        if confidence < 60:
            print(f"  {symbol}: Confidence too low ({confidence}%)")
            return None
        
        return {
            'symbol': symbol,
            'direction': direction,
            'entry': entry,
            'sl': sl,
            'tp1': tp1,
            'tp2': tp2,
            'confidence': confidence,
            'atr_pct': atr_pct
        }
    
    except Exception as e:
        print(f"  {symbol}: Error - {e}")
        return None


def build_message(signal):
    pair = signal['symbol'].replace('/', '')
    direction = signal['direction']
    
    msg = f"""🦋 Heikin Ashi Signal 🦋

Pairs : {pair} 
Position: {direction}
Leverage: Cross {LEVERAGE}x 
Entry: {_fmt(signal['entry'])}

SL: {_fmt(signal['sl'])}

Targets
TP1 - {_fmt(signal['tp1'])}
TP2 - {_fmt(signal['tp2'])}
-----------
Trade on Weex - Win up to 10k 🔥
https://www.weex.com/register?vipCode=0s0t4s"""
    
    return msg


def main():
    print("=" * 50)
    print("Heikin Ashi Algo Signals — Bot (v1.0)")
    print(f"Time (UTC): {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    
    if not TELEGRAM_TOKEN or not CHANNEL_ID:
        print("ERROR: TELEGRAM_TOKEN and CHANNEL_ID must be set!")
        return
    
    if not in_trading_window():
        print(f"Outside trading window (allowed: 00:00–{TRADING_WINDOW_HOURS:02d}:00 UTC). Exiting.")
        return
    
    top_coins = get_top_mexc_coins(50)
    if not top_coins:
        print("No coins fetched. Aborting.")
        return
    
    cooldown_data = load_cooldown()
    signals_sent = 0
    skipped_cooldown = 0
    skipped_filter = 0
    ha_bias_cache = {}
    
    print("\nScanning coins...")
    for symbol in top_coins:
        try:
            if is_on_cooldown(symbol, cooldown_data):
                skipped_cooldown += 1
                continue
            
            # تحديد الاتجاه من شموع Heikin Ashi
            if symbol not in ha_bias_cache:
                time.sleep(0.3)
                ha_bias_cache[symbol] = get_ha_bias(symbol)
            
            direction = ha_bias_cache[symbol]
            if direction is None:
                skipped_filter += 1
                continue
            
            time.sleep(0.6)
            signal = analyze_symbol(symbol, direction)
            
            if signal is None:
                skipped_filter += 1
                continue
            
            msg = build_message(signal)
            send_telegram(msg)
            cooldown_data[symbol] = datetime.now().isoformat()
            signals_sent += 1
            print(f"  ✅ SIGNAL SENT: {symbol} {signal['direction']} (Conf: {signal['confidence']}%)")
            time.sleep(1.5)
        
        except Exception as e:
            print(f"  {symbol}: Exception - {e}")
    
    save_cooldown(cooldown_data)
    print(f"\n🏁 Done. Signals: {signals_sent} | Cooldown skipped: {skipped_cooldown} | Filter skipped: {skipped_filter}")


if __name__ == "__main__":
    main()
