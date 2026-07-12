#!/usr/bin/env python3
"""因子数据库浏览器 — 后端API服务器"""
import json, os, sqlite3, urllib.parse, urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

DB = '/workspace/factor_engine/factor_store.db'
HOST, PORT = '0.0.0.0', 7410
HTML_FILE = '/workspace/factor_viewer.html'

# ── 因子分类定义 ──
CATEGORIES = [
    {"id": "ma", "label": "均线系统", "icon": "📊",
     "cols": ["ma5","ma10","ma20","ma60","ema12","ema26","ema9"]},
    {"id": "tech", "label": "技术指标", "icon": "📈",
     "cols": ["rsi6","rsi12","rsi24","macd","atr14",
              "williams_r","bias_6","cci_20"]},
    {"id": "boll", "label": "布林带", "icon": "📉",
     "cols": ["boll_upper","boll_middle","boll_lower","boll_bw","boll_pos"]},
    {"id": "momentum", "label": "动量收益", "icon": "🚀",
     "cols": ["ret_1d","ret_5d","ret_20d","ret_60d","high_52w_pct",
              "roc_12","ema9_trend"]},
    {"id": "volume", "label": "量价关系", "icon": "📊",
     "cols": ["volume_ratio","turnover_rate","turnover_ratio",
              "volume_change","amihud","vp_confirm"]},
    {"id": "flow", "label": "资金流向", "icon": "💧",
     "cols": ["flow_1d","flow_5d","flow_20d","flow_trend",
              "obv","vpt"]},
    {"id": "volatility", "label": "波动率风险", "icon": "🌊",
     "cols": ["hv_20","dv_20","amplitude","max_dd",
              "sharpe_20","sortino_20"]},
    {"id": "valuation", "label": "估值", "icon": "💰",
     "cols": ["pe_ttm","pb","ps","pcf","dividend_yield",
              "bps","ttm_eps","peg","profit_growth"]},
    {"id": "msci", "label": "MSCI六因子", "icon": "🏆",
     "cols": ["msci_value","msci_momentum","msci_quality",
              "msci_lowvol","msci_size","msci_yield",
              "msci_composite","msci_level"]},
    {"id": "financial", "label": "财务健康", "icon": "📋",
     "cols": ["debt_ratio","equity_multiplier","roa","roe",
              "net_margin","cash_quality","asset_turnover",
              "revenue_growth","health_score","health_level",
              "current_ratio","payout_ratio"]},
    {"id": "pattern", "label": "形态信号", "icon": "🔮",
     "cols": ["ma_arrange","vp_confirm","entry_score","entry_level",
              "entry_detail","trend_score","trend_level","trend_detail"]},
    {"id": "score", "label": "综合评分", "icon": "⭐",
     "cols": ["val_score","val_level","entry_score","entry_level",
              "trend_score","trend_level","liq_score","liq_level"]},
]

ALL_CATEGORY_COLS = set()
for cat in CATEGORIES:
    ALL_CATEGORY_COLS.update(cat['cols'])
ALL_CATEGORY_COLS.update(['date','symbol','name'])

def get_conn():
    conn = sqlite3.connect(DB, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

class FactorAPI(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    
    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode())
    
    def _html(self, content):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(content.encode())
    
    def _serve_static(self, path):
        """Serve static files from workspace"""
        safe = os.path.normpath(os.path.join('/workspace', path.lstrip('/')))
        if not safe.startswith('/workspace/'):
            self.send_error(403)
            return
        if not os.path.exists(safe):
            self.send_error(404)
            return
        ext = os.path.splitext(safe)[1]
        ct = {'html': 'text/html', 'js': 'text/javascript', 
              'css': 'text/css', 'png': 'image/png',
              'ico': 'image/x-icon'}.get(ext[1:], 'application/octet-stream')
        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.end_headers()
        with open(safe, 'rb') as f:
            self.wfile.write(f.read())
    
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip('/')
        params = urllib.parse.parse_qs(parsed.query)
        
        # ── Serve HTML ──
        if path == '' or path == '/factor_viewer' or path == '/index.html':
            self._html(open(HTML_FILE).read())
            return
        if path == '/call_ai':
            self._html(open('/workspace/call_ai_form.html').read())
            return
        
        # ── Static files ──
        if path.startswith('/static/'):
            self._serve_static(path)
            return
            
        # ── API: categories ──
        if path == '/api/categories':
            self._json(CATEGORIES)
            return
        
        # ── API: stocks list ──
        if path == '/api/stocks':
            conn = get_conn()
            rows = conn.execute(
                "SELECT DISTINCT symbol, name FROM factor_snapshots ORDER BY symbol"
            ).fetchall()
            conn.close()
            self._json([{"symbol": r['symbol'], "name": r['name']} for r in rows])
            return
        
        # ── API: dates list ──
        if path == '/api/dates':
            conn = get_conn()
            rows = conn.execute(
                "SELECT DISTINCT date FROM factor_snapshots ORDER BY date DESC"
            ).fetchall()
            conn.close()
            self._json([r['date'] for r in rows])
            return
        
        # ── API: factor data (paginated) ──
        if path == '/api/factors':
            symbols = params.get('symbols', [''])[0]
            dt_from = params.get('date_from', [''])[0]
            dt_to = params.get('date_to', [''])[0]
            page = int(params.get('page', ['1'])[0])
            limit = int(params.get('limit', ['20'])[0])
            cols_param = params.get('cols', [''])[0]
            offset = (page - 1) * limit
            
            # Build query
            where = []
            args = []
            if symbols:
                sym_list = [s.strip() for s in symbols.split(',') if s.strip()]
                if sym_list:
                    placeholders = ','.join(['?'] * len(sym_list))
                    where.append(f"symbol IN ({placeholders})")
                    args.extend(sym_list)
            if dt_from:
                where.append("date >= ?")
                args.append(dt_from)
            if dt_to:
                where.append("date <= ?")
                args.append(dt_to)
            
            where_clause = ' AND '.join(where) if where else '1=1'
            
            # Select columns
            if cols_param:
                select_cols = ['date', 'symbol', 'name'] + [c.strip() for c in cols_param.split(',') if c.strip()]
            else:
                select_cols = ['date', 'symbol', 'name']
            
            # Validate columns exist
            conn = get_conn()
            all_cols = [r[1] for r in conn.execute("PRAGMA table_info(factor_snapshots)").fetchall()]
            select_cols = [c for c in select_cols if c in all_cols]
            
            col_str = ','.join(select_cols)
            
            # Count total
            count = conn.execute(
                f"SELECT COUNT(*) FROM factor_snapshots WHERE {where_clause}", args
            ).fetchone()[0]
            
            # Fetch page
            rows = conn.execute(
                f"SELECT {col_str} FROM factor_snapshots WHERE {where_clause} ORDER BY date DESC, symbol LIMIT ? OFFSET ?",
                args + [limit, offset]
            ).fetchall()
            conn.close()
            
            self._json({
                "total": count,
                "page": page,
                "limit": limit,
                "total_pages": max(1, (count + limit - 1) // limit),
                "columns": select_cols,
                "rows": [dict(r) for r in rows]
            })
            return
        
        # ── Proxy to daemon :7408（同源代理，解决跨域）──
        if path.startswith('/daemon/'):
            import urllib.request as daemon_req
            target = 'http://127.0.0.1:7408/' + path[len('/daemon/'):]
            if parsed.query:
                target += '?' + parsed.query
            try:
                if self.command == 'GET':
                    r = daemon_req.urlopen(target, timeout=45)
                    data = r.read()
                    ct = r.headers.get('Content-Type', 'application/json')
                    r.close()
                else:
                    body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                    req = daemon_req.Request(target, data=body, method=self.command,
                        headers={'Content-Type': self.headers.get('Content-Type', 'application/json')})
                    r = daemon_req.urlopen(req, timeout=45)
                    data = r.read()
                    ct = r.headers.get('Content-Type', 'application/json')
                    r.close()
                self.send_response(r.status)
                self.send_header('Content-Type', ct)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
            except urllib.error.HTTPError as e:
                self.send_response(e.code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(e.read())
            except Exception as e:
                self._json({'error': str(e)}, 502)
            return
        
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip('/')
        if path.startswith('/daemon/'):
            import urllib.request as daemon_req
            target = 'http://127.0.0.1:7408/' + path[len('/daemon/'):]
            try:
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                req = daemon_req.Request(target, data=body, method='POST',
                    headers={'Content-Type': self.headers.get('Content-Type', 'application/json')})
                r = daemon_req.urlopen(req, timeout=30)
                data = r.read()
                ct = r.headers.get('Content-Type', 'application/json')
                r.close()
                self.send_response(r.status)
                self.send_header('Content-Type', ct)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
            except urllib.error.HTTPError as e:
                self.send_response(e.code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(e.read())
            except Exception as e:
                self._json({'error': str(e)}, 502)
            return
        self._json({"error": "?"}, 404)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path.rstrip('/')
        if path.startswith('/daemon/'):
            import urllib.request as daemon_req
            target = 'http://127.0.0.1:7408/' + path[len('/daemon/'):]
            try:
                req = daemon_req.Request(target, method='DELETE')
                r = daemon_req.urlopen(req, timeout=15)
                data = r.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._json({'error': str(e)}, 502)
            return
        self._json({"error": "?"}, 404)

def main():
    print(f"因子浏览器: http://{HOST}:{PORT}/factor_viewer")
    print(f"  股票数: 查询中...")
    conn = get_conn()
    stocks = conn.execute("SELECT COUNT(DISTINCT symbol) FROM factor_snapshots").fetchone()[0]
    dates = conn.execute("SELECT COUNT(DISTINCT date) FROM factor_snapshots").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM factor_snapshots").fetchone()[0]
    conn.close()
    print(f"  股票: {stocks}只  日期: {dates}天  总记录: {total}条")
    print(f"  因子: 93列 | 12个分类")
    
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind((HOST, PORT))
    sock.listen(128)
    server = HTTPServer((HOST, PORT), FactorAPI, bind_and_activate=False)
    server.socket = sock
    server.server_address = (HOST, PORT)
    server.serve_forever()

if __name__ == '__main__':
    main()

def run_server():
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind((HOST, PORT))
    sock.listen(128)
    server = HTTPServer((HOST, PORT), FactorAPI, bind_and_activate=False)
    server.socket = sock
    server.server_address = (HOST, PORT)
    print(f"因子浏览器: http://{HOST}:{PORT}/factor_viewer")
