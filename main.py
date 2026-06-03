import asyncio
import json
import time
import os
import urllib.request
import websockets
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from urllib.parse import urlparse, parse_qs
from collections import deque

# ── Config ──
TG_TOKEN = os.environ.get('TG_TOKEN', '8992735363:AAEKSGYpeEAAcMc6rTf_yysRWQ59l9k_8D8')
TG_CHAT  = os.environ.get('TG_CHAT',  '236032472')

config = {
    'btc_pct':  float(os.environ.get('BTC_PCT', '0.3')),   # BTC min rise % in 5 min
    'vol_mult': float(os.environ.get('VOL_MULT', '3.0')),   # Volume multiplier
    'alt_pct':  float(os.environ.get('ALT_PCT', '0.5')),    # Altcoin min rise %
    'mode':     os.environ.get('MODE', 'multi'),             # multi or spike
    'spike_pct': float(os.environ.get('SPIKE_PCT', '5.0')), # spike mode %
    'spike_sec': float(os.environ.get('SPIKE_SEC', '1.0')), # spike mode seconds
}

# ── State ──
btc_prices = deque(maxlen=600)   # last 10 min of BTC trades
alt_prices = {}                   # sym -> deque of (price, time)
last_tg = {}                      # sym -> last alert time
alert_count = 0
start_time = time.time()

# ── Telegram ──
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = json.dumps({'chat_id': TG_CHAT, 'text': msg}).encode()
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=8)
        print(f"TG: {msg[:60]}")
    except Exception as e:
        print(f"TG error: {e}")

# ── BTC Analysis ──
def btc_5m_change():
    if len(btc_prices) < 2: return 0
    now = time.time()
    old = next((p for p in btc_prices if p[1] >= now - 300), None)
    if not old: return 0
    current = btc_prices[-1]
    return ((current[0] - old[0]) / old[0]) * 100

def btc_vol_ratio():
    if len(btc_prices) < 30: return 1.0
    now = time.time()
    last_1m = [p for p in btc_prices if p[1] >= now - 60]
    prev_9m = [p for p in btc_prices if now - 600 <= p[1] < now - 60]
    if not last_1m or not prev_9m: return 1.0
    curr_vol = sum(p[2] for p in last_1m)
    avg_vol = sum(p[2] for p in prev_9m) / max(len(prev_9m) / max(len(last_1m), 1), 1)
    return curr_vol / avg_vol if avg_vol > 0 else 1.0

# ── Multi-signal check ──
def check_multi_signal(sym, price, ts):
    global alert_count

    btc_change = btc_5m_change()
    vol_ratio = btc_vol_ratio()

    # Condition 1: BTC rising
    c1 = btc_change >= config['btc_pct']
    # Condition 2: Volume spike
    c2 = vol_ratio >= config['vol_mult']

    if not c1 or not c2:
        return

    # Condition 3: Altcoin moving
    prices = alt_prices.get(sym)
    if not prices or len(prices) < 5:
        return

    recent = list(prices)[-5:]
    alt_change = ((recent[-1][0] - recent[0][0]) / recent[0][0]) * 100
    c3 = alt_change >= config['alt_pct']

    # Condition 4: Altcoin independent (moved with or before BTC peak)
    alt_move_time = recent[-1][1] / 1000
    btc_last_time = btc_prices[-1][1] if btc_prices else time.time()
    c4 = alt_move_time <= btc_last_time + 10

    if not (c3 and c4):
        return

    # All 4 conditions met
    now = time.time()
    last_sent = last_tg.get(sym, 0)
    if now - last_sent < 60:
        return

    last_tg[sym] = now
    alert_count += 1

    name = sym.replace('USDT', '')
    btc_price = btc_prices[-1][0] if btc_prices else 0
    decimals = 8 if price < 0.001 else 6 if price < 0.01 else 5 if price < 1 else 4 if price < 100 else 2

    msg = (
        f"🎯 إشارة متعددة — {name}/USDT\n"
        f"━━━━━━━━━━━━━━━\n"
        f"✅ BTC ارتفع: +{btc_change:.3f}% (5د)\n"
        f"✅ حجم BTC: {vol_ratio:.1f}x المعدل\n"
        f"✅ {name} تحرك: +{alt_change:.3f}%\n"
        f"✅ مستقل عن BTC\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 سعر {name}: ${price:.{decimals}f}\n"
        f"₿ BTC: ${btc_price:,.0f}\n"
        f"⏱ {time.strftime('%H:%M:%S')}"
    )
    threading.Thread(target=send_telegram, args=(msg,), daemon=True).start()

# ── Spike check ──
prev_spike = {}

def check_spike(sym, price, ts):
    global alert_count
    prev = prev_spike.get(sym)
    now = ts / 1000 if ts > 1e10 else ts

    if not prev:
        prev_spike[sym] = {'price': price, 'time': now}
        return

    dt = now - prev['time']
    win = config['spike_sec']

    if 0 < dt <= win * 1.5:
        pct = ((price - prev['price']) / prev['price']) * 100
        if pct >= config['spike_pct']:
            last_sent = last_tg.get(sym, 0)
            if now - last_sent >= 30:
                last_tg[sym] = now
                alert_count += 1
                name = sym.replace('USDT', '')
                decimals = 8 if price < 0.001 else 6 if price < 0.01 else 5 if price < 1 else 4 if price < 100 else 2
                msg = (
                    f"🚀 ارتفاع مفاجئ — {name}/USDT\n"
                    f"+{pct:.3f}% خلال {win}ث\n"
                    f"${prev['price']:.{decimals}f} → ${price:.{decimals}f}\n"
                    f"⏱ {time.strftime('%H:%M:%S')}"
                )
                threading.Thread(target=send_telegram, args=(msg,), daemon=True).start()

    if dt >= win:
        prev_spike[sym] = {'price': price, 'time': now}

# ── Pairs ──
PAIRS = [
    'btcusdt','ethusdt','bnbusdt','solusdt','xrpusdt','dogeusdt','adausdt','avaxusdt',
    'trxusdt','tonusdt','shibusdt','dotusdt','linkusdt','maticusdt','ltcusdt','bchusdt',
    'nearusdt','uniusdt','aptusdt','icpusdt','xlmusdt','suiusdt','atomusdt','etcusdt',
    'filusdt','injusdt','arbusdt','opusdt','hbarusdt','mkrusdt','aaveusdt','stxusdt',
    'imxusdt','sandusdt','manausdt','axsusdt','grtusdt','ftmusdt','rndrusdt','wldusdt',
    'tiausdt','seiusdt','jupusdt','enausdt','dymusdt','pythusdt','jtousdt','pepeusdt',
    'wifusdt','bomeusdt','flokiusdt','ordiusdt','runeusdt','ldousdt','cakeusdt','gmtusdt',
    'flowusdt','algousdt','vetusdt','iotausdt','compusdt','yfiusdt','sushiusdt','1inchusdt',
    'balusdt','crvusdt','snxusdt','zrxusdt','kavausdt','oceanusdt','ankrusdt','sklusdt',
    'hntusdt','roseusdt','iotxusdt','woousdt','fetusdt','agixusdt','nmrusdt','maskusdt',
    'storjusdt','kncusdt','powrusdt','dentusdt','winusdt','chrusdt','belusdt','eosusdt',
    'galausdt','gmxusdt','highusdt','idusdt','joeusdt','lrcusdt','magicusdt','cfxusdt',
    'reefusdt','renusdt','rvnusdt','scusdt','spellusdt','superusdt','truusdt','umausdt',
    'voxelusdt','xemusdt','xmrusdt','xtzusdt','yggusdt','zenusdt','ambusdt','antusdt',
    'audiousdt','bntusdt','c98usdt','celousdt','ctkusdt','cvcusdt','cvxusdt','darusdt',
    'duskusdt','funusdt','hookusdt','klayusdt','litusdt','phbusdt','radusdt','sfpusdt',
    'stmxusdt','sxpusdt','tkousdt','utkusdt','vibusdt','wtcusdt','xvsusdt','zilusdt',
    'blzusdt','bandusdt','cotiusdt','celrusdt','icxusdt','altusdt','aerousdt','agldusdt',
    'apeusdt','egldusdt','ensusdt','ethfiusdt','galusdt','glmusdt','iousdt','kasusdt',
    'kdausdt','lptusdt','lqtyusdt','ondousdt','pendleusdt','rdntusdt','sagausdt','stgusdt',
    'strkusdt','sunusdt','thetausdt','twtusdt','wbtcusdt','xaiusdt','zecusdt','zetausdt',
    'zkusdt','zrousdt','notusdt','taousdt','peopleusdt','arusdt','portalusdt','wusdt',
]

# ── WebSocket ──
async def watch_chunk(pairs, chunk_id):
    streams = '/'.join(f"{p}@aggTrade" for p in pairs)
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
                print(f"Chunk {chunk_id}: connected ({len(pairs)} pairs)")
                async for msg in ws:
                    try:
                        d = json.loads(msg).get('data', {})
                        if d.get('e') != 'aggTrade': continue
                        sym = d['s']
                        price = float(d['p'])
                        ts = d['T']
                        now = time.time()

                        if sym == 'BTCUSDT':
                            btc_prices.append((price, now, float(d['q'])))
                        else:
                            if sym not in alt_prices:
                                alt_prices[sym] = deque(maxlen=300)
                            alt_prices[sym].append((price, ts))

                            if config['mode'] == 'multi':
                                check_multi_signal(sym, price, ts)
                            else:
                                check_spike(sym, price, ts)
                    except Exception:
                        pass
        except Exception as e:
            print(f"Chunk {chunk_id} error: {e}, retry in 3s...")
            await asyncio.sleep(3)

# ── HTTP server ──
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        updated = []
        if 'btc_pct' in params:
            config['btc_pct'] = float(params['btc_pct'][0]); updated.append(f"btc_pct={config['btc_pct']}")
        if 'vol_mult' in params:
            config['vol_mult'] = float(params['vol_mult'][0]); updated.append(f"vol_mult={config['vol_mult']}")
        if 'alt_pct' in params:
            config['alt_pct'] = float(params['alt_pct'][0]); updated.append(f"alt_pct={config['alt_pct']}")
        if 'mode' in params:
            config['mode'] = params['mode'][0]; updated.append(f"mode={config['mode']}")
        if 'pct' in params:
            config['spike_pct'] = float(params['pct'][0]); updated.append(f"spike_pct={config['spike_pct']}")
        if 'sec' in params:
            config['spike_sec'] = float(params['sec'][0]); updated.append(f"spike_sec={config['spike_sec']}")

        if updated:
            print(f"Config updated: {', '.join(updated)}")

        uptime = int(time.time() - start_time)
        response = json.dumps({
            'status': 'running',
            'mode': config['mode'],
            'btc_tracked': len(btc_prices) > 0,
            'alts_tracked': len(alt_prices),
            'alerts_sent': alert_count,
            'uptime_seconds': uptime,
            'config': config
        }, ensure_ascii=False).encode()

        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *args): pass

def run_server():
    port = int(os.environ.get('PORT', 8080))
    HTTPServer(('0.0.0.0', port), Handler).serve_forever()

# ── Main ──
async def main():
    mode_ar = 'متعدد الإشارات' if config['mode'] == 'multi' else 'ارتفاع مفاجئ'
    print(f"Starting | mode: {config['mode']} | pairs: {len(PAIRS)}")
    send_telegram(
        f"✅ Spike Bot يعمل الآن\n"
        f"الوضع: {mode_ar}\n"
        f"العملات: {len(PAIRS)}\n\n"
        f"لتغيير الوضع:\n"
        f"متعدد: ?mode=multi\n"
        f"مفاجئ: ?mode=spike&pct=5&sec=1"
    )

    ALL = ['btcusdt'] + [p for p in PAIRS if p != 'btcusdt']
    chunks = [ALL[i:i+50] for i in range(0, len(ALL), 50)]
    await asyncio.gather(*[watch_chunk(c, i) for i, c in enumerate(chunks)])

if __name__ == '__main__':
    threading.Thread(target=run_server, daemon=True).start()
    asyncio.run(main())
