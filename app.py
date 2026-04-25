import os, json, math, sqlite3
from datetime import datetime, date
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import anthropic
import yfinance as yf
from scipy.stats import norm

app = Flask(__name__, static_folder='static')
CORS(app)

DB_PATH = 'journal.db'
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# ── Database ──────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                raw_analysis TEXT NOT NULL,
                ticker TEXT,
                direction TEXT,
                strike REAL,
                option_type TEXT,
                expiration TEXT,
                timeframe TEXT,
                catalysts TEXT,
                conviction INTEGER,
                estimated_premium REAL,
                contracts INTEGER DEFAULT 1,
                entry_price REAL,
                status TEXT DEFAULT 'open',
                exit_price REAL,
                exit_at TEXT,
                pnl REAL,
                pnl_pct REAL,
                tesis_correct INTEGER,
                post_analysis TEXT,
                conversation TEXT
            );
            CREATE TABLE IF NOT EXISTS price_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER,
                recorded_at TEXT,
                price REAL,
                FOREIGN KEY(trade_id) REFERENCES trades(id)
            );
        ''')

init_db()

# ── Black-Scholes ─────────────────────────────────────────────────────────
def bs_call_price(S, K, T, r=0.045, sigma=0.185):
    if T <= 0: return max(S - K, 0)
    d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)
    return S*norm.cdf(d1) - K*math.exp(-r*T)*norm.cdf(d2)

def bs_put_price(S, K, T, r=0.045, sigma=0.185):
    if T <= 0: return max(K - S, 0)
    d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)
    return K*math.exp(-r*T)*norm.cdf(-d2) - S*norm.cdf(-d1)

def get_live_price(ticker):
    try:
        t = yf.Ticker(ticker)
        data = t.history(period='1d', interval='1m')
        if not data.empty:
            return float(data['Close'].iloc[-1])
        info = t.fast_info
        return float(info.last_price)
    except:
        return None

def estimate_premium(ticker, strike, option_type, timeframe, sigma=0.185):
    price = get_live_price(ticker)
    if not price or not strike: return None
    hours = {'0dte': 6.5, 'hoy': 6.5, 'esta semana': 32.5, 'semana': 32.5, 'este mes': 130}.get(
        timeframe.lower() if timeframe else '', 6.5)
    T = hours / (252 * 6.5)
    if option_type and option_type.lower() == 'put':
        return round(bs_put_price(price, strike, T, sigma=sigma), 2)
    return round(bs_call_price(price, strike, T, sigma=sigma), 2)

# ── Claude helpers ────────────────────────────────────────────────────────
def claude_extract_trade(raw_text, current_price=None):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    price_context = f" El precio actual del activo mencionado es ${current_price:.2f}." if current_price else ""
    
    resp = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=1000,
        system="""Eres un asistente de trading que extrae información estructurada de análisis en lenguaje natural.
Responde SOLO con JSON válido, sin texto adicional, sin markdown.
Extrae estos campos:
- ticker: símbolo del activo (SPY, QQQ, AAPL, etc). Si no se menciona explícitamente, infiere del contexto.
- direction: "call" o "put"
- strike: número del strike si se menciona, null si no
- option_type: "call" o "put"
- timeframe: "0dte", "esta semana", "este mes", u otro
- catalysts: lista de strings con los catalizadores identificados
- conviction: número del 1 al 10 inferido del tono (palabras como "bastante convencido"=7, "creo que"=5, "seguro"=8)
- contracts: número de contratos si se menciona, 1 por defecto
- missing_fields: lista de campos críticos que faltan y que deberías preguntar. Solo incluye: "strike" si no hay strike, "ticker" si no está claro, "contracts" si quiere saber cuántos. NO preguntes por timeframe si es obvio que es hoy.
- followup_question: UNA sola pregunta natural y corta para obtener los missing_fields. null si no faltan campos críticos. NO sugieras strikes ni des argumentos sobre la estrategia.""",
        messages=[{'role': 'user', 'content': f"Análisis del trader: {raw_text}{price_context}"}]
    )
    try:
        text = resp.content[0].text.strip()
        text = text.replace('```json','').replace('```','').strip()
        return json.loads(text)
    except:
        return {}

def claude_post_analysis(trade_data, price_history):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=800,
        system="""Eres un asistente de trading que analiza operaciones pasadas de forma objetiva y concisa.
Tu rol es explicar POR QUÉ salió bien o mal la operación basándote en los datos.
NO sugieras cambios de estrategia. NO des opiniones sobre si debería seguir operando.
Analiza: 1) Si el catalizador se cumplió 2) El timing 3) El movimiento del precio vs la tesis.
Sé directo y específico. Máximo 150 palabras.""",
        messages=[{'role': 'user', 'content': f"""
Trade: {json.dumps(trade_data, ensure_ascii=False)}
Historial de precios durante el trade: {json.dumps(price_history)}
P&L final: {trade_data.get('pnl_pct', 0):.1f}%
La tesis se cumplió: {'Sí' if trade_data.get('tesis_correct') else 'No'}
Analiza por qué salió así."""}]
    )
    return resp.content[0].text.strip()

def claude_stats_analysis(trades_summary):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=600,
        system="""Analiza las estadísticas de trading de forma objetiva. 
Identifica patrones: qué tipo de catalizadores funcionan más, en qué timeframes hay mejor hit rate, etc.
NO des consejos de gestión de riesgo no solicitados. Solo describe patrones en los datos.
Máximo 200 palabras.""",
        messages=[{'role': 'user', 'content': f"Estadísticas: {json.dumps(trades_summary, ensure_ascii=False)}"}]
    )
    return resp.content[0].text.strip()

# ── API Routes ────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/analyze', methods=['POST'])
def analyze():
    data = request.json
    raw = data.get('text', '')
    conversation = data.get('conversation', [])
    
    # Get price for context
    ticker_hint = data.get('ticker_hint')
    current_price = get_live_price(ticker_hint) if ticker_hint else None
    
    extracted = claude_extract_trade(raw, current_price)
    
    # If we have ticker but no price yet, get it
    if extracted.get('ticker') and not current_price:
        current_price = get_live_price(extracted['ticker'])
    
    # Estimate premium if we have enough data
    premium = None
    if extracted.get('ticker') and extracted.get('strike') and extracted.get('timeframe'):
        premium = estimate_premium(
            extracted['ticker'], 
            extracted['strike'],
            extracted.get('option_type', 'call'),
            extracted['timeframe']
        )
        extracted['estimated_premium'] = premium
    
    if current_price:
        extracted['current_price'] = current_price
    
    return jsonify({
        'extracted': extracted,
        'followup': extracted.get('followup_question'),
        'ready': not bool(extracted.get('missing_fields'))
    })

@app.route('/api/trades', methods=['POST'])
def create_trade():
    data = request.json
    extracted = data.get('extracted', {})
    raw = data.get('raw_analysis', '')
    conversation = json.dumps(data.get('conversation', []))
    
    now = datetime.now().isoformat()
    
    # Get entry price
    entry_price = None
    ticker = extracted.get('ticker')
    if ticker:
        entry_price = get_live_price(ticker)
    
    with get_db() as db:
        cur = db.execute('''
            INSERT INTO trades (created_at, raw_analysis, ticker, direction, strike, option_type,
                expiration, timeframe, catalysts, conviction, estimated_premium, contracts,
                entry_price, status, conversation)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (
            now, raw, ticker,
            extracted.get('direction', extracted.get('option_type')),
            extracted.get('strike'),
            extracted.get('option_type', 'call'),
            extracted.get('expiration'),
            extracted.get('timeframe'),
            json.dumps(extracted.get('catalysts', [])),
            extracted.get('conviction'),
            extracted.get('estimated_premium'),
            extracted.get('contracts', 1),
            entry_price,
            'open',
            conversation
        ))
        trade_id = cur.lastrowid
        
        # Save initial price snapshot
        if entry_price:
            db.execute('INSERT INTO price_snapshots (trade_id, recorded_at, price) VALUES (?,?,?)',
                      (trade_id, now, entry_price))
    
    return jsonify({'id': trade_id, 'entry_price': entry_price})

@app.route('/api/trades', methods=['GET'])
def get_trades():
    with get_db() as db:
        trades = db.execute('''
            SELECT * FROM trades ORDER BY created_at DESC
        ''').fetchall()
    
    result = []
    for t in trades:
        d = dict(t)
        d['catalysts'] = json.loads(d['catalysts'] or '[]')
        result.append(d)
    return jsonify(result)

@app.route('/api/trades/<int:trade_id>', methods=['GET'])
def get_trade(trade_id):
    with get_db() as db:
        trade = db.execute('SELECT * FROM trades WHERE id=?', (trade_id,)).fetchone()
        snapshots = db.execute('SELECT * FROM price_snapshots WHERE trade_id=? ORDER BY recorded_at',
                              (trade_id,)).fetchall()
    if not trade:
        return jsonify({'error': 'not found'}), 404
    d = dict(trade)
    d['catalysts'] = json.loads(d['catalysts'] or '[]')
    d['price_history'] = [dict(s) for s in snapshots]
    return jsonify(d)

@app.route('/api/trades/<int:trade_id>/close', methods=['POST'])
def close_trade(trade_id):
    data = request.json
    tesis_correct = data.get('tesis_correct', False)
    
    with get_db() as db:
        trade = db.execute('SELECT * FROM trades WHERE id=?', (trade_id,)).fetchone()
        if not trade:
            return jsonify({'error': 'not found'}), 404
        
        trade = dict(trade)
        ticker = trade['ticker']
        exit_price = get_live_price(ticker) if ticker else None
        now = datetime.now().isoformat()
        
        # Calculate P&L
        pnl = None
        pnl_pct = None
        if exit_price and trade['entry_price'] and trade['strike']:
            entry_premium = trade['estimated_premium'] or 0
            T_remaining = 0.01 / (252 * 6.5)
            
            if trade['option_type'] == 'call':
                exit_premium = bs_call_price(exit_price, trade['strike'], T_remaining)
            else:
                exit_premium = bs_put_price(exit_price, trade['strike'], T_remaining)
            
            contracts = trade['contracts'] or 1
            pnl = (exit_premium - entry_premium) * 100 * contracts
            pnl_pct = ((exit_premium - entry_premium) / entry_premium * 100) if entry_premium > 0 else 0
        
        # Save final snapshot
        if exit_price:
            db.execute('INSERT INTO price_snapshots (trade_id, recorded_at, price) VALUES (?,?,?)',
                      (trade_id, now, exit_price))
        
        # Get price history for post analysis
        snapshots = db.execute('SELECT recorded_at, price FROM price_snapshots WHERE trade_id=? ORDER BY recorded_at',
                              (trade_id,)).fetchall()
        price_history = [dict(s) for s in snapshots]
        
        # Generate post analysis
        trade['pnl_pct'] = pnl_pct or 0
        trade['tesis_correct'] = tesis_correct
        trade['catalysts'] = json.loads(trade['catalysts'] or '[]')
        post_analysis = claude_post_analysis(trade, price_history)
        
        db.execute('''
            UPDATE trades SET status='closed', exit_price=?, exit_at=?, 
            pnl=?, pnl_pct=?, tesis_correct=?, post_analysis=?
            WHERE id=?
        ''', (exit_price, now, pnl, pnl_pct, 1 if tesis_correct else 0, post_analysis, trade_id))
    
    return jsonify({'pnl': pnl, 'pnl_pct': pnl_pct, 'post_analysis': post_analysis})

@app.route('/api/trades/<int:trade_id>/snapshot', methods=['POST'])
def add_snapshot(trade_id):
    with get_db() as db:
        trade = db.execute('SELECT ticker FROM trades WHERE id=?', (trade_id,)).fetchone()
        if not trade: return jsonify({'error': 'not found'}), 404
        price = get_live_price(trade['ticker'])
        if price:
            db.execute('INSERT INTO price_snapshots (trade_id, recorded_at, price) VALUES (?,?,?)',
                      (trade_id, datetime.now().isoformat(), price))
    return jsonify({'price': price})

@app.route('/api/stats', methods=['GET'])
def get_stats():
    with get_db() as db:
        trades = db.execute("SELECT * FROM trades WHERE status='closed'").fetchall()
    
    if not trades:
        return jsonify({'message': 'No hay trades cerrados aún'})
    
    trades = [dict(t) for t in trades]
    total = len(trades)
    winners = sum(1 for t in trades if (t['pnl'] or 0) > 0)
    tesis_ok = sum(1 for t in trades if t['tesis_correct'])
    total_pnl = sum(t['pnl'] or 0 for t in trades)
    avg_pnl_pct = sum(t['pnl_pct'] or 0 for t in trades) / total if total else 0
    
    # By catalyst
    catalyst_stats = {}
    for t in trades:
        cats = json.loads(t['catalysts'] or '[]')
        won = (t['pnl'] or 0) > 0
        for cat in cats:
            if cat not in catalyst_stats:
                catalyst_stats[cat] = {'wins': 0, 'total': 0}
            catalyst_stats[cat]['total'] += 1
            if won: catalyst_stats[cat]['wins'] += 1
    
    # By conviction
    conviction_stats = {}
    for t in trades:
        c = t['conviction'] or 0
        bucket = f"{(c//3)*3+1}-{(c//3)*3+3}"
        if bucket not in conviction_stats:
            conviction_stats[bucket] = {'wins': 0, 'total': 0}
        conviction_stats[bucket]['total'] += 1
        if (t['pnl'] or 0) > 0:
            conviction_stats[bucket]['wins'] += 1
    
    summary = {
        'total_trades': total,
        'win_rate': round(winners/total*100, 1),
        'tesis_hit_rate': round(tesis_ok/total*100, 1),
        'total_pnl': round(total_pnl, 2),
        'avg_pnl_pct': round(avg_pnl_pct, 1),
        'catalyst_stats': catalyst_stats,
        'conviction_stats': conviction_stats,
        'recent_trades': [{'ticker': t['ticker'], 'pnl_pct': t['pnl_pct'], 
                          'conviction': t['conviction'], 'catalysts': json.loads(t['catalysts'] or '[]')} 
                         for t in trades[-10:]]
    }
    
    ai_analysis = claude_stats_analysis(summary)
    summary['ai_analysis'] = ai_analysis
    
    return jsonify(summary)

@app.route('/api/price/<ticker>', methods=['GET'])
def get_price(ticker):
    price = get_live_price(ticker)
    return jsonify({'ticker': ticker, 'price': price})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
