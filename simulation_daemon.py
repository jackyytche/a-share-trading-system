#!/usr/bin/env python3
"""
仿真交易守护进程 v8 — SQLite存储 + 10种条件单

Changes from v7:
  - 所有数据存入 SQLite (simulation.db)，废弃 simulation_data.json
  - 5张表：accounts / balance_logs / positions / trades / conditions
  - 撤销条件单 → status='cancelled'（物理不删除）
  - 资金变更写入 balance_logs（审计追踪）
  - WAL模式支持并发读
"""
import json, os, sqlite3, threading, signal, sys, time, socket
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.parse import urlparse

# ── 配置 ──
DB_PATH = os.environ.get("SIM_DB_PATH", "/workspace/simulation.db")
TENCENT_API = "https://qt.gtimg.cn/q="
QMT_BASE = "http://192.168.2.220:7403"
HOST, PORT = "127.0.0.1", 7408
REFRESH_SEC = 5.0
DEFAULT_CASH = 861928.0

# ── 链式AI冷却参数 ──
COOLING_MIN_MOVE_PCT = 0.5      # 价格变动≥0.5%才唤醒AI
COOLING_MIN_SEC = 120           # 距上次唤醒至少120秒

# 全局AI冷却状态（按标的code，跨条件单）
_ai_cooling = {}
_ai_cooling_lock = threading.Lock()

# 时区设为北京时间
os.environ["TZ"] = "Asia/Shanghai"
try:
    time.tzset()
except: pass

# ── DB写锁（防HTTP handler与条件引擎写冲突）──
_db_write_lock = threading.RLock()

def db_write(fn):
    """装饰器：所有写DB的函数加锁"""
    def wrapper(*a, **kw):
        with _db_write_lock:
            return fn(*a, **kw)
    return wrapper

# ── 交易限制配置（可通过API动态调整）──
TRADING_RESTRICTIONS = {
    "max_cash_usage_pct": 80,          # 最大可用资金占总资金百分比
    "max_single_position_pct": 30,     # 单只标的最高仓位占比
    "max_positions": 10,               # 同时最大持仓数量
    "min_cash_reserve_pct": 5,         # 最低保留现金（占总资产百分比）
    "allow_batch_buy": True,           # 是否允许分批建仓
    "batch_max_count": 3,              # 分批次数上限
    "allow_batch_sell": True,          # 是否允许分批卖出
    "price_slippage_pct": 0.5,         # 可接受价格偏离百分比
    "stop_loss_default_pct": 8,        # 默认止损百分比
    "min_shares": 100,                 # 最小交易股数
    "round_lot": 100,                  # 交易单位（一手=100股）
    "day_trade_limit": False,          # A股T+1限制（True=遵守）
}

def get_trading_restrictions_text():
    """返回格式化的交易限制说明，供AI决策参考"""
    r = TRADING_RESTRICTIONS
    return (
        f"## 当前交易限制\n"
        f"- 最大可用资金: {r['max_cash_usage_pct']}%（总资金{r['max_cash_usage_pct']}%以内）\n"
        f"- 单标的最大仓位: {r['max_single_position_pct']}%\n"
        f"- 同时最大持仓数: {r['max_positions']}只\n"
        f"- 最低保留现金: {r['min_cash_reserve_pct']}%（占总资产）\n"
        f"- 分批建仓: {'允许' if r['allow_batch_buy'] else '禁止'}（最多{r['batch_max_count']}次）\n"
        f"- 分批卖出: {'允许' if r['allow_batch_sell'] else '禁止'}\n"
        f"- 可接受价格偏离: ±{r['price_slippage_pct']}%\n"
        f"- 默认止损: {r['stop_loss_default_pct']}%\n"
        f"- 交易单位: {r['round_lot']}股/手\n"
        f"- T+1限制: {'遵守' if r['day_trade_limit'] else '不限制（仿真）'}\n"
    )
CONFIG_PATH = os.environ.get("HERMES_CONFIG", "/home/hermeswebui/.hermes/config.yaml")
LLM_CONFIG = {"base_url": "", "api_key": "", "model": ""}

def load_llm_config():
    """从config.yaml读取当前provider的LLM配置"""
    try:
        import yaml
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        
        m = cfg.get("model", {})
        provider_raw = m.get("provider", "")
        model_name = m.get("model", "")
        
        # 解析 provider: "custom:deepseek" → "deepseek"
        provider_name = provider_raw.split(":", 1)[-1] if ":" in provider_raw else provider_raw
        
        # 在 custom_providers 里按 name 查找
        for cp in cfg.get("custom_providers", []):
            if cp.get("name") == provider_name:
                LLM_CONFIG["base_url"] = cp.get("base_url", "").rstrip("/")
                LLM_CONFIG["api_key"] = cp.get("api_key", "")
                LLM_CONFIG["model"] = model_name or cp.get("model", "")
                return True
        return False
    except Exception as e:
        print(f"⚠️ 读取LLM配置失败: {e}", flush=True)
        return False

def call_llm(messages):
    """调用大模型，返回响应文本"""
    if not LLM_CONFIG["base_url"]:
        load_llm_config()
    
    url = f"{LLM_CONFIG['base_url']}/v1/chat/completions"
    body = json.dumps({
        "model": LLM_CONFIG["model"],
        "messages": messages,
        "stream": False,
        "max_tokens": 2000,
    }).encode()
    
    req = Request(url, data=body, headers={
        "Authorization": f"Bearer {LLM_CONFIG['api_key']}",
        "Content-Type": "application/json",
    })
    try:
        resp = urlopen(req, timeout=60)
        data = json.loads(resp.read())
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        return f"[LLM调用失败] {e}"

# 启动时加载LLM配置
load_llm_config()

# ── AI System Prompt ──

AI_SYSTEM_PROMPT = """你是一个A股仿真交易AI决策引擎。当条件单的价格触发时，会先执行条件单本身的交易，然后叫你做后续决策。

## 你可以下的条件单类型（共8种call_ai_版）

你下的所有条件单都会自动加上call_ai_前缀。普通版（price_trigger/bounce_buy等）是给用户手动设置的，触发后直接交易，不会叫你。

| 序号 | 你使用的类型 | 说明 | 关键参数 |
|:----|:-----------|:------|:--------|
| 1 | call_ai_price_trigger | 定价交易 | trigger=lte/gte, trigger_price |
| 2 | call_ai_bounce_buy | 先跌到监控价→再反弹买入 | monitor_price, rebound_pct |
| 3 | call_ai_pullback_sell | 先涨到监控价→再回落卖出 | monitor_price, pullback_pct |
| 4 | call_ai_opened_sell | 涨停打开后回落卖 | fallback_pct |
| 5 | call_ai_grid | 区间网格高抛低吸 | grid_low, grid_high, grid_step, grid_max_shares |
| 6 | call_ai_breakeven_sell | 回本就卖 | 无额外参数 |
| 7 | call_ai_time_trigger | 定时+价条件交易 | trigger_time, trigger_price |
| 8 | call_ai_t_trade | 做T | t_type=buy_first/sell_first, t_spread |

## 你可以返回的 action（共3种）

1. **place_conditions**: 下新条件单（可同时下多条），链继续。
   **每次被叫时都必须下至少一条条件单，否则链会断裂，该标的将无人管理。**
2. **skip**: 取消关注，停止管理此标的。
   **仅当该标的已清仓（持仓=0）时使用。如果有持仓却skip，该标的将无人管理，风险由用户承担。**
3. **request_factor**: 请求查看该标的完整因子数据（93列含MSCI/技术/估值/财务）。
   **看完因子数据后必须重新决策（place_conditions或skip），不得再请求。**

## 输出格式

必须返回纯JSON（无markdown、无代码块标记）：

```json
{
  "analysis": "一句话分析理由",
  "action": "place_conditions",
  "conditions": [
    {
      "type": "call_ai_price_trigger",
      "code": "601138",
      "side": "buy",
      "trigger": "gte",
      "trigger_price": 63.00,
      "shares": 100,
      "name": "工业富联",
      "note": "AI的决策理由",
      "valid_until": "2026-08-07"
    }
  ]
}

action=skip时conditions为空数组。
action=place_conditions时conditions至少1条。

## 约束层级（Constraint Levels）

用户可以在策略备注中标记约束层级，决定你的决策自由度：

| 标记 | 含义 | 你的自由度 |
|:----|:-----|:----------|
| `[固定@价格]` | 硬约束，必须在指定价格执行 | 价格不可变，方向和数量可微调 |
| `[区间@低价-高价]` | 软约束，在区间内自由决策 | 价格可在区间内调整 |
| `[动态]` | 无价格约束，完全由你判断 | 完全自由，基于因子数据决策 |
| 无标记 | 默认硬约束（向后兼容） | 保持当前行为，受原始策略约束 |

**约束优先级：** 硬约束 > 软约束 > 动态。下新条件时如果违反硬约束，系统会自动修正到约束价格。
"""

FACTOR_DB = "/workspace/factor_engine/factor_store.db"

def parse_constraints(note):
    """从用户策略备注解析约束层级
    返回: {"level": "fixed"/"range"/"dynamic", "fixed_price": N, "range_low": N, "range_high": N}
    """
    if not note:
        return {"level": "fixed"}
    result = {"level": "fixed"}
    import re
    m = re.search(r'\[固定[@]?([\d.]+)\]', note)
    if m:
        result["level"] = "fixed"
        result["fixed_price"] = float(m.group(1))
        return result
    m = re.search(r'\[区间[@]?([\d.]+)\s*[-–]\s*([\d.]+)\]', note)
    if m:
        result["level"] = "range"
        result["range_low"] = float(m.group(1))
        result["range_high"] = float(m.group(2))
        return result
    m = re.search(r'\[动态\]', note)
    if m:
        result["level"] = "dynamic"
        return result
    # 无标记 → 尝试从原点位推断固定约束（向后兼容）
    return result

def _get_factor_ref(code):
    """从因子库获取标的的EMA9/MSCI/技术因子参考（含空值防护）"""
    try:
        conn = sqlite3.connect(FACTOR_DB, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        row = conn.execute(
            "SELECT date, ema9, ema9_trend, rsi6, macd, ma_arrange,"
            " msci_quality, msci_momentum, msci_value, msci_lowvol, msci_size, msci_yield,"
            " msci_composite, entry_score, trend_score"
            " FROM factor_snapshots WHERE symbol=? AND ema9 IS NOT NULL"
            " ORDER BY date DESC LIMIT 1",
            (code,)
        ).fetchone()
        conn.close()
        if not row:
            return "  (因子数据暂缺 — 收盘后自动更新)"
        cols = ["date","ema9","ema9_trend","rsi6","macd","ma_arrange",
                "msci_quality","msci_momentum","msci_value","msci_lowvol",
                "msci_size","msci_yield","msci_composite","entry_score","trend_score"]
        d = dict(zip(cols, row))
        # 空值防护：所有数值/字符串字段转安全值
        def _v(v, fmt="num"):
            if v is None: return "?"
            if fmt == "num": return v
            return v
        lines = []
        lines.append(f"  EMA9={_v(d['ema9'])}（{_v(d['ema9_trend'])}）RSI6={_v(d['rsi6'])} MACD={_v(d['macd'])}")
        lines.append(f"  均线排列: {d['ma_arrange'] or '?'}  |  MSCI综合: {_v(d['msci_composite'])}")
        lines.append(f"  MSCI: 质量{_v(d['msci_quality'])} 动量{_v(d['msci_momentum'])} 价值{_v(d['msci_value'])} ")
        lines.append(f"        低波{_v(d['msci_lowvol'])} 规模{_v(d['msci_size'])} 股息{_v(d['msci_yield'])}")
        lines.append(f"  做多分值{_v(d['entry_score'])} 趋势分值{_v(d['trend_score'])}  |  数据日期: {d['date']}")
        return "\n".join(lines)
    except Exception:
        return "  (因子数据暂缺)"


def _get_full_factor(code):
    """获取指定标的的全量93列因子数据，格式化为多行字符串供AI参考"""
    try:
        conn = sqlite3.connect(FACTOR_DB, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        cols = [d[1] for d in conn.execute("PRAGMA table_info(factor_snapshots)").fetchall()]
        row = conn.execute(
            "SELECT * FROM factor_snapshots WHERE symbol=? AND ema9 IS NOT NULL"
            " ORDER BY date DESC LIMIT 1",
            (code,)
        ).fetchone()
        conn.close()
        if not row:
            return "(该标的暂无因子数据 — 收盘后自动更新)"
        d = dict(zip(cols, row))
        lines = [f"标的: {code}  {d.get('name','')}  |  数据日期: {d.get('date','?')}"]
        groups = {
            "实时行情": ["price","price_change","market_cap"],
            "技术因子": ["ma5","ma10","ma20","ma60","ema12","ema26","rsi6","rsi12","rsi24",
                       "macd","boll_upper","boll_middle","boll_lower","boll_bw","atr14",
                       "volume_ratio","ma_arrange","boll_pos","vp_confirm"],
            "EMA9/周线": ["ema9","ema9_trend","weekly_close"],
            "收益率/动量": ["ret_1d","ret_5d","ret_20d","ret_60d","high_52w_pct","roc_12"],
            "波动率/风险": ["hv_20","dv_20","amplitude","max_dd","sharpe_20",
                         "williams_r","bias_6","cci_20","obv","vpt"],
            "估值": ["pe_ttm","pb","ps","pcf","dividend_yield","peg","ttm_eps","profit_growth"],
            "财务": ["roe","roa","debt_ratio","equity_multiplier","net_margin",
                    "cash_quality","asset_turnover","revenue_growth","health_score"],
            "资金流向": ["flow_1d","flow_5d","flow_20d","flow_trend"],
            "MSCI六因子": ["msci_value","msci_momentum","msci_quality","msci_lowvol",
                         "msci_size","msci_yield","msci_composite","msci_level"],
            "综合评分": ["entry_score","entry_level","trend_score","trend_level","val_score","val_level"],
            "流动性": ["turnover_rate","turnover_ratio","volume_change","amihud","liq_score","liq_level","current_ratio"],
        }
        for gname, fields in groups.items():
            items = [f"{fn}={d[fn]}" for fn in fields if d.get(fn) is not None]
            if items:
                lines.append(f"[{gname}] {'  '.join(items)}")
        return "\n".join(lines)
    except Exception as e:
        return f"(因子数据查询失败: {e})"


def build_ai_context(trigger_cond, code, price, now_str, quote=None):
    """构建AI决策所需上下文"""
    # 如果没有传入quote，自己取（兜底）
    if quote is None:
        try:
            quote = pe.get_quote(code)
        except:
            quote = {}
    # 获取各持仓的清仓日（从条件单的valid_until）+ 仓位上限
    clear_dates = {}
    pos_limits = {}
    try:
        conn = get_conn()
        for r in conn.execute("SELECT code, valid_until, params FROM conditions WHERE account_id=1 AND status='active'"):
            if r['valid_until'] and r['side'] == 'buy':
                cd = r['valid_until']
                if r['code'] not in clear_dates or cd < clear_dates[r['code']]:
                    clear_dates[r['code']] = cd
            try:
                p = json.loads(r['params']) if isinstance(r['params'], str) else (r['params'] or {})
                if 'max_position_pct' in p:
                    pos_limits[r['code']] = p['max_position_pct']
            except: pass
        conn.close()
    except: pass
    
    # 当前持仓
    pos_list = []
    for c, p in get_positions_dict().items():
        line = f"  {p['name']}({c}): {p['shares']}股 @{p['avg_cost']}"
        if c in clear_dates:
            cd = clear_dates[c]
            today = datetime.now().strftime("%Y-%m-%d")
            if cd < today:
                line += f" ⚠️清仓日已过({cd})"
            else:
                line += f" [清仓日:{cd}]"
        if c in pos_limits:
            line += f" [仓位上限:{pos_limits[c]}%]"
        pos_list.append(line)
    pos_str = "\n".join(pos_list) if pos_list else "  空仓"
    
    return (
        f"## 触发信息\n"
        f"条件: {trigger_cond.get('cid','')}\n"
        f"类型: {trigger_cond.get('type','')}\n"
        f"标的: {code} {trigger_cond.get('name','')}\n"
        f"当前价: {price}\n"
        f"触发参数: trigger={trigger_cond.get('trigger','')} price={trigger_cond.get('trigger_price','')}\n"
        f"备注: {trigger_cond.get('note','')}\n"
        f"约束: {({'fixed':'固定','range':'区间','dynamic':'动态'}).get(trigger_cond.get('internal_state',{}).get('_constraint',{}).get('level','fixed'),'固定')} "
        f"{trigger_cond.get('internal_state',{}).get('_constraint',{}).get('fixed_price','') or ''}"
        f"{(' @'+str(trigger_cond.get('internal_state',{}).get('_constraint',{}).get('range_low',''))+'-'+str(trigger_cond.get('internal_state',{}).get('_constraint',{}).get('range_high',''))) if trigger_cond.get('internal_state',{}).get('_constraint',{}).get('level')=='range' else ''}"
        f"\n"
        f"原始策略: {trigger_cond.get('internal_state',{}).get('_origin_note','（首环，无上游）')}\n\n"
        f"## 实时行情\n"
        f"- 涨跌: {quote.get('change_pct','?')}%  |  换手: {quote.get('turnover_rate','?')}%\n"
        f"- 量比: {quote.get('volume_ratio','?')}  |  振幅: {quote.get('amplitude','?')}%\n"
        f"- 量: {quote.get('volume_lots',0)/10000:.0f}万手  |  内外盘比: {quote.get('outer_disc',0)}/{quote.get('inner_disc',0)}={quote.get('outer_disc',0)/(quote.get('inner_disc',1) or 1):.2f}\n"
        f"- 昨收:{quote.get('pre_close','?')}  今开:{quote.get('open','?')}  高:{quote.get('high','?')}  低:{quote.get('low','?')}\n"
        f"- 涨停:{quote.get('up_limit','?')}  跌停:{quote.get('down_limit','?')}\n"
        f"\n"
        f"## 五档盘口\n"
        f"  卖5 {quote.get('ask_prices',[0])[4]:.2f} × {quote.get('ask_vols',[0])[4]}手\n"
        f"  卖4 {quote.get('ask_prices',[0])[3]:.2f} × {quote.get('ask_vols',[0])[3]}手\n"
        f"  卖3 {quote.get('ask_prices',[0])[2]:.2f} × {quote.get('ask_vols',[0])[2]}手\n"
        f"  卖2 {quote.get('ask_prices',[0])[1]:.2f} × {quote.get('ask_vols',[0])[1]}手\n"
        f"  卖1 {quote.get('ask_prices',[0])[0]:.2f} × {quote.get('ask_vols',[0])[0]}手\n"
        f"  ────────────\n"
        f"  当前 {price:.2f}\n"
        f"  ────────────\n"
        f"  买1 {quote.get('bid_prices',[0])[0]:.2f} × {quote.get('bid_vols',[0])[0]}手\n"
        f"  买2 {quote.get('bid_prices',[0])[1]:.2f} × {quote.get('bid_vols',[0])[1]}手\n"
        f"  买3 {quote.get('bid_prices',[0])[2]:.2f} × {quote.get('bid_vols',[0])[2]}手\n"
        f"  买4 {quote.get('bid_prices',[0])[3]:.2f} × {quote.get('bid_vols',[0])[3]}手\n"
        f"  买5 {quote.get('bid_prices',[0])[4]:.2f} × {quote.get('bid_vols',[0])[4]}手\n"
        f"\n"
        f"## 实时估值\n"
        f"- PE(动态): {quote.get('pe','?')}   |   PB: {quote.get('pb','?')}\n"
        f"- 总市值: {quote.get('market_cap',0):.0f}亿\n"
        f"\n"
        f"## 因子参考\n{_get_factor_ref(code)}\n"
        f"\n"
        f"## 当前持仓\n{pos_str}\n\n"
        f"## 当前现金\n{get_cash():.2f}\n\n"
        f"## 本条件单交易限制（以本条件单为准，覆盖全局限制）\n"
        f"- 分批建仓: {'允许' if trigger_cond.get('allow_batch',True) else '禁止'}"
        f"{'（最多'+str(trigger_cond.get('batch_max_count',3))+'次）' if trigger_cond.get('allow_batch',True) else ''}\n"
        f"- 分批卖出: {'允许' if trigger_cond.get('allow_batch_sell',True) else '禁止'}\n"
        f"- 可接受价格偏离: ±{trigger_cond.get('price_slippage_pct',0.5)}%\n"
        f"- 默认止损: {trigger_cond.get('stop_loss_default_pct',8)}%\n"
        f"- 交易单位: {trigger_cond.get('round_lot',100)}股/手\n"
        f"- 留底资金: {trigger_cond.get('min_cash_reserve_pct',5)}%（占总资产）\n\n"
        f"## 全局交易限制（仅当本条件单未设时生效）\n"
        f"{get_trading_restrictions_text()}\n"
        f"## 请输出JSON决策"
    )

@db_write
def execute_ai_decision(ai_resp, trigger_cid, now, price, chain_id=None, seq=1, llm_time=None, trade_executed=False, decision=None):
    """解析AI返回的JSON并执行决策"""
    import re
    t0 = time.time()
    
    # 查找触发条件信息
    chain_info = {"code": "", "side": ""}
    try:
        conn = get_conn()
        r = conn.execute("SELECT code, side FROM conditions WHERE cid=?", [trigger_cid]).fetchone()
        if r:
            chain_info = dict(r)
        conn.close()
    except: pass
    
    # 如果没有chain_id，用trigger_cid本身
    if not chain_id:
        chain_id = trigger_cid
    
    # 解析JSON（如果外部已解析好，直接用）
    if decision is None:
        json_str = ai_resp.strip()
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', json_str, re.DOTALL)
        if m:
            json_str = m.group(1)
        try:
            decision = json.loads(json_str)
        except json.JSONDecodeError:
            start = json_str.find('{')
            end = json_str.rfind('}')
            if start >= 0 and end > start:
                try:
                    decision = json.loads(json_str[start:end+1])
                except:
                    print(f"  ⚠️ AI响应JSON解析失败", flush=True)
                    return False
            else:
                print(f"  ⚠️ AI响应JSON解析失败", flush=True)
                return False
    
    action = decision.get("action", "place_conditions")
    analysis = decision.get("analysis", "")
    conds = decision.get("conditions", [])
    
    # 记录analysis到日志
    print(f"  🤖 AI决策: {action} | {analysis[:80]}", flush=True)
    
    # 追加到888.txt日志
    try:
        with open("/workspace/888.txt", "a", encoding="utf-8") as f:
            ts = now.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"\n[{ts}] 触发: {trigger_cid}\n")
            f.write(f"  决策: {action}\n")
            f.write(f"  分析: {analysis}\n")
            for c in conds:
                f.write(f"  下发: {c.get('type','')} {c.get('code','')} @{c.get('trigger_price','')} {c.get('note','')[:50]}\n")
    except: pass
    
    # 记录AI统计（交易已在唤醒AI前执行完成）
    is_trade = 1 if trade_executed else 0
    try:
        conn = get_conn()
        conn.execute(
            """INSERT INTO ai_stats (chain_id, sequence, trigger_cid, code, price, action, is_trade, analysis, conditions_count, response_time, llm_response)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [chain_id, seq, trigger_cid, chain_info.get("code",""), price, 
             action, is_trade, analysis[:200], len(conds), llm_time, ai_resp[:500]]
        )
        conn.commit()
        conn.close()
    except: pass
    
    if action == "skip":
        # 当前条件单已标记triggered
        code = chain_info.get("code", "")
        shares = pos_shares(code) if code else 0
        if code and shares > 0:
            # 持仓>0不能skip → 转为wait保其他条件继续监控
            action = "wait"
            print(f"  ⚠️ AI请求skip但{code}仍持{shares}股，自动转为wait", flush=True)
        elif code and shares == 0:
            # 已清仓 → 清理残留，链正常结束
            _cancel_conditions_for_code(code, exclude_cid=trigger_cid)
        return True

    if action == "wait":
        # wait: 不创建新条件。如果有其他活跃条件会继续监控，否则链结束
        return True
    
    if action == "place_conditions":
        # 清理同标的旧AI条件：只取消同类型+同方向的旧条件，保留用户的原始链首条件
        cleanup_code = chain_info.get("code", "")
        if cleanup_code and conds:
            # 收集本轮要下的条件"类型+方向"组合
            new_type_sigs = set()
            for c in conds:
                base = c.get("type", "price_trigger").replace("call_ai_", "", 1)
                side = c.get("side", "buy")
                new_type_sigs.add((base, side))
            try:
                conn_clean = get_conn()
                for base_type, side in new_type_sigs:
                    # 取消同标的、同类型、同方向、活跃的AI条件（排除链首和当前触发）
                    conn_clean.execute(
                        "UPDATE conditions SET status='cancelled', updated_at=datetime('now','localtime') "
                        "WHERE account_id=1 AND code=? AND status='active' "
                        "AND type LIKE ? AND side=? AND cid!=? "
                        "AND cid!=?",
                        [cleanup_code, f"call_ai_{base_type}%", side, trigger_cid, chain_id]
                    )
                affected = conn_clean.execute("SELECT changes()").fetchone()[0]
                conn_clean.commit()
                conn_clean.close()
                if affected:
                    print(f"  🧹 清理了 {cleanup_code} 的 {affected} 条同类型旧AI条件", flush=True)
            except Exception as e:
                print(f"  ⚠️ 清理旧条件失败: {e}", flush=True)

        for c in conds:
            try:
                code = c.get("code", "")
                ctype = c.get("type", "price_trigger")
                side = c.get("side", "buy")
                # 链式AI：所有条件强制call_ai_（先执行交易再唤起AI）
                if not ctype.startswith("call_ai_"):
                    ctype = "call_ai_" + ctype
                cid = c.get("cid", f"ai-{code}-{int(time.time())}")
                shares = int(c.get("shares", 100))
                # note优先用原始策略（链追溯），确保AI不会遗忘用户指令
                note = c.get("note", f"AI: {analysis[:50]}")
                # 追溯原始策略提示词（链源头第一条条件单的note）
                origin_note = ""
                constraint = {"level": "fixed"}
                if chain_id:
                    try:
                        conn_orig = get_conn()
                        r2 = conn_orig.execute("SELECT note, internal_state FROM conditions WHERE cid=? AND account_id=1", [chain_id]).fetchone()
                        conn_orig.close()
                        if r2 and r2[0]:
                            origin_note = r2[0][:200]
                        if r2 and r2[1]:
                            try:
                                ist = json.loads(r2[1])
                                if "_constraint" in ist:
                                    constraint = ist["_constraint"]
                            except: pass
                    except: pass
                # 约束层：如果AI下的是价格触发条件，检查是否违反硬约束
                trigger_price = c.get("trigger_price", 0)
                if trigger_price and constraint.get("level") == "fixed" and constraint.get("fixed_price"):
                    if abs(trigger_price - constraint["fixed_price"]) / max(constraint["fixed_price"], 0.01) > 0.02:
                        old_tp = trigger_price
                        trigger_price = constraint["fixed_price"]
                        c["trigger_price"] = trigger_price
                        print(f"  ⚠️ AI触发价{old_tp}偏离约束{constraint['fixed_price']}，已修正", flush=True)
                elif trigger_price and constraint.get("level") == "range":
                    low = constraint.get("range_low", 0)
                    high = constraint.get("range_high", 0)
                    if low and high:
                        if trigger_price < low:
                            c["trigger_price"] = low
                            print(f"  ⚠️ AI触发价{trigger_price}低于区间{low}，已修正到下限", flush=True)
                        elif trigger_price > high:
                            c["trigger_price"] = high
                            print(f"  ⚠️ AI触发价{trigger_price}高于区间{high}，已修正到上限", flush=True)
                if origin_note:
                    note = origin_note
                valid_until = c.get("valid_until", "2026-08-07")
                name = c.get("name", f"股票{code}")
                trigger = c.get("trigger", "gte")
                
                # 构建params
                params = {"trigger": trigger}
                if ctype.endswith("price_trigger") or ctype == "call_ai_price_trigger":
                    params["trigger_price"] = float(c.get("trigger_price", 0))
                elif "bounce" in ctype:
                    params["monitor_price"] = float(c.get("monitor_price", 0))
                    params["rebound_pct"] = float(c.get("rebound_pct", 2))
                elif "pullback" in ctype:
                    params["monitor_price"] = float(c.get("monitor_price", 0))
                    params["pullback_pct"] = float(c.get("pullback_pct", 3))
                elif ctype == "opened_sell" or ctype == "call_ai_opened_sell":
                    params["fallback_pct"] = float(c.get("fallback_pct", 3))
                elif "grid" in ctype:
                    params.update({k: float(c.get(k,0)) for k in ["grid_low","grid_high","grid_step","grid_max_shares"]})
                elif "time" in ctype:
                    params["trigger_time"] = c.get("trigger_time", "14:55")
                    params["trigger_price"] = float(c.get("trigger_price", 0))
                elif "t_trade" in ctype:
                    params["t_type"] = c.get("t_type", "buy_first")
                    params["t_spread"] = float(c.get("t_spread", 1.0))
                
                conn = get_conn()
                conn.execute(
                    "INSERT INTO conditions (account_id, cid, code, name, type, side, status, params, shares, valid_until, note, internal_state)"
                    " VALUES (1,?,?,?,?,?,'active',?,?,?,?,?)",
                    [cid, code, name, ctype, side, json.dumps(params), shares,
                     valid_until or None, note,
                     json.dumps({"_chain_id": chain_id, "_seq": seq + 1, "_origin": "ai",
                                "_origin_note": origin_note, "_constraint": constraint})]
                )
                conn.commit()
                conn.close()
                print(f"  ✅ AI下发条件单: {cid} ({ctype} {code})", flush=True)
            except Exception as e:
                print(f"  ⚠️ AI下发条件单失败: {e}", flush=True)
        return True
    
    return False


def _call_ai_after_trade(cond, code, cp, now, quote, internal, trade_ok=True):
    """条件单交易执行后，调用AI做后续决策（含价格敏感冷却 + request_factor + 动作约束）"""
    now_s = time.time()
    with _ai_cooling_lock:
        last = _ai_cooling.get(code, {})
        last_price = last.get("price", 0)
        last_time = last.get("time", 0)
        pct_move = abs(cp - last_price) / max(last_price, 0.01) * 100 if last_price > 0 else 999
        since = now_s - last_time if last_time > 0 else 999
        if pct_move < COOLING_MIN_MOVE_PCT and since < COOLING_MIN_SEC:
            return  # 冷却中，跳过AI
        _ai_cooling[code] = {"price": cp, "time": now_s}
    
    chain_id = internal.get("_chain_id") or cond["cid"]
    seq = internal.get("_seq", 1)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    import re as _re
    
    for round_n in range(2):  # 第0轮=首次，第1轮=request_factor重试
        user_msg = build_ai_context(cond, code, cp, now_str, quote) if round_n == 0 else (
            f"## 完整因子数据（您请求查看）\n{factor_data}\n\n"
            f"以上是该标的的全部因子数据。请基于此数据做出决策：\n"
            f"- place_conditions: 下条件单\n"
            f"- skip: 结束管理（仅当已清仓时）")
        messages = [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ] if round_n == 0 else messages  # request_factor轮用追加后的messages
        
        ai_t0 = time.time()
        ai_resp = call_llm(messages)
        llm_time = round(time.time() - ai_t0, 3)
        
        # 解析JSON
        try:
            text = ai_resp.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text and text.count("```") >= 2:
                text = text.split("```")[1].split("```")[0]
            js_start = text.find("{")
            js_end = text.rfind("}") + 1
            if js_start >= 0 and js_end > js_start:
                decision = json.loads(text[js_start:js_end])
            else:
                decision = {"action": "place_conditions", "analysis": "JSON解析失败，默认继续", "conditions": []}
        except:
            decision = {"action": "place_conditions", "analysis": "JSON解析异常", "conditions": []}
        
        action = decision.get("action", "place_conditions")
        
        # request_factor处理
        if action == "request_factor":
            if round_n == 0:
                factor_data = _get_full_factor(code)
                messages.append({"role": "assistant", "content": ai_resp})
                messages.append({"role": "user", "content": 
                    f"## 完整因子数据（您请求查看）\n{factor_data}\n\n"
                    f"请基于以上数据重新决策（place_conditions或skip）。"})
                continue  # 第1轮重试
            else:
                # 第二轮还request_factor → 强制place_conditions防丢失
                action = "place_conditions"
                decision["action"] = "place_conditions"
                decision["analysis"] = (decision.get("analysis","") + " [因子已提供，自动转为place_conditions]")
                print(f"  ⚠️ AI第二轮仍request_factor，强制place_conditions", flush=True)
        
        # 传给execute_ai_decision（带已解析的decision，避免重复解析）
        execute_ai_decision(ai_resp, cond["cid"], now, cp, chain_id, seq, llm_time, trade_ok, decision)
        return


# ── 数据库层 ──

SCHEMA_CASH = 861928.0

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL DEFAULT '默认仿真账户',
    cash        REAL    NOT NULL DEFAULT {SCHEMA_CASH},
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS balance_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    delta         REAL    NOT NULL,
    balance_after REAL    NOT NULL,
    trade_id      INTEGER,
    reason        TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    code        TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    shares      INTEGER NOT NULL DEFAULT 0,
    avg_cost    REAL    NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(account_id, code)
);
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    code          TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    side          TEXT    NOT NULL CHECK(side IN ('buy','sell')),
    price         REAL    NOT NULL,
    shares        INTEGER NOT NULL,
    amount        REAL    NOT NULL,
    profit        REAL,
    trigger_note  TEXT,
    condition_id  INTEGER,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS conditions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    cid             TEXT    NOT NULL,
    code            TEXT    NOT NULL,
    name            TEXT,
    type            TEXT    NOT NULL,
    side            TEXT    NOT NULL CHECK(side IN ('buy','sell','both')),
    status          TEXT    NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','triggered','expired','cancelled')),
    params          TEXT    NOT NULL DEFAULT '{{}}',
    shares          INTEGER NOT NULL DEFAULT 0,
    valid_until     TEXT,
    note            TEXT,
    trigger_price   REAL,
    triggered_at    TEXT,
    internal_state  TEXT    DEFAULT '{{}}',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(account_id, cid)
);
CREATE INDEX IF NOT EXISTS idx_bal_log_account ON balance_logs(account_id);
CREATE INDEX IF NOT EXISTS idx_pos_account     ON positions(account_id);
CREATE INDEX IF NOT EXISTS idx_trades_account  ON trades(account_id);
CREATE INDEX IF NOT EXISTS idx_trades_code     ON trades(code);
CREATE INDEX IF NOT EXISTS idx_trades_time     ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_cond_account    ON conditions(account_id);
CREATE INDEX IF NOT EXISTS idx_cond_code       ON conditions(code);
CREATE INDEX IF NOT EXISTS idx_cond_status     ON conditions(status);
CREATE TABLE IF NOT EXISTS ai_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id        TEXT    NOT NULL,
    sequence        INTEGER NOT NULL DEFAULT 1,
    trigger_cid     TEXT,
    code            TEXT,
    price           REAL,
    action          TEXT    NOT NULL,
    is_trade        INTEGER DEFAULT 0,
    analysis        TEXT,
    conditions_count INTEGER DEFAULT 0,
    response_time   REAL,
    llm_response    TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_ai_chain ON ai_stats(chain_id);
CREATE INDEX IF NOT EXISTS idx_ai_code  ON ai_stats(code);
"""


def get_conn():
    """每个线程/调用各自连接，WAL模式不阻塞"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def db_init():
    """建表 + 首次自动创建默认账户"""
    conn = get_conn()
    conn.executescript(SCHEMA_SQL)
    row = conn.execute("SELECT id FROM accounts WHERE id=1").fetchone()
    if not row:
        conn.execute("INSERT INTO accounts (id, name, cash) VALUES (1, '默认仿真账户', ?)", [DEFAULT_CASH])
        conn.execute("INSERT INTO balance_logs (account_id, delta, balance_after, reason) VALUES (1,?,?,'init')",
                     [DEFAULT_CASH, DEFAULT_CASH])
        conn.commit()
        # 初始持仓
        conn.execute("INSERT INTO positions (account_id, code, name, shares, avg_cost) VALUES (1,'601138','工业富联',850,65.32)")
        conn.commit()
    conn.close()


# ── 领域操作 ──

def ensure_account():
    """确保1号账户存在，返回cash"""
    conn = get_conn()
    row = conn.execute("SELECT cash FROM accounts WHERE id=1").fetchone()
    if row:
        cash = row["cash"]
        conn.close()
        return cash
    conn.execute("INSERT INTO accounts (id, name, cash) VALUES (1, '默认仿真账户', ?)", [DEFAULT_CASH])
    conn.execute("INSERT INTO balance_logs (account_id, delta, balance_after, reason) VALUES (1,?,?,'init')",
                 [DEFAULT_CASH, DEFAULT_CASH])
    conn.commit()
    conn.close()
    return DEFAULT_CASH


def get_cash():
    conn = get_conn()
    row = conn.execute("SELECT cash FROM accounts WHERE id=1").fetchone()
    conn.close()
    return row["cash"] if row else ensure_account()


def update_cash(delta, reason, trade_id=None):
    """更新现金并写balance_log"""
    conn = get_conn()
    conn.execute("UPDATE accounts SET cash = cash + ?, updated_at = datetime('now','localtime') WHERE id=1", [delta])
    cash = conn.execute("SELECT cash FROM accounts WHERE id=1").fetchone()["cash"]
    conn.execute("INSERT INTO balance_logs (account_id, delta, balance_after, trade_id, reason) VALUES (1,?,?,?,?)",
                 [delta, cash, trade_id, reason])
    conn.commit()
    conn.close()
    return cash


def get_positions_dict():
    """返回 {code: {name, shares, avg_cost}}"""
    conn = get_conn()
    rows = conn.execute("SELECT code, name, shares, avg_cost FROM positions WHERE account_id=1 AND shares>0").fetchall()
    conn.close()
    return {r["code"]: {"name": r["name"], "shares": r["shares"], "avg_cost": r["avg_cost"]} for r in rows}


def get_position(code):
    conn = get_conn()
    row = conn.execute("SELECT * FROM positions WHERE account_id=1 AND code=?", [code]).fetchone()
    conn.close()
    return dict(row) if row else None


def has_position(code):
    p = get_position(code)
    return p is not None and p["shares"] > 0


def pos_shares(code):
    p = get_position(code)
    return p["shares"] if p else 0


def pos_cost(code):
    p = get_position(code)
    return p["avg_cost"] if p else 0


def _cancel_conditions_for_code(code, exclude_cid=None):
    """取消某标的所有活跃条件（排除正在触发的那个）"""
    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT cid FROM conditions WHERE account_id=1 AND code=? AND status='active'",
            [code]
        ).fetchall()
        for (cid,) in rows:
            if exclude_cid and cid == exclude_cid:
                continue
            conn.execute("UPDATE conditions SET status='cancelled' WHERE cid=?", [cid])
        conn.commit()
        conn.close()
    except:
        pass


def upsert_position(code, name, shares, avg_cost):
    conn = get_conn()
    conn.execute(
        """INSERT INTO positions (account_id, code, name, shares, avg_cost, updated_at)
           VALUES (1,?,?,?,?,datetime('now','localtime'))
           ON CONFLICT(account_id,code) DO UPDATE SET
             shares=excluded.shares, avg_cost=excluded.avg_cost, updated_at=excluded.updated_at""",
        [code, name, shares, avg_cost]
    )
    conn.commit()
    conn.close()


def remove_position_if_zero(code):
    conn = get_conn()
    conn.execute("DELETE FROM positions WHERE account_id=1 AND code=? AND shares<=0", [code])
    conn.commit()
    conn.close()


def add_trade(code, name, side, price, shares, amount, profit=None, trigger_note="", condition_id=None):
    conn = get_conn()
    cursor = conn.execute(
        "INSERT INTO trades (account_id, code, name, side, price, shares, amount, profit, trigger_note, condition_id) VALUES (1,?,?,?,?,?,?,?,?,?)",
        [code, name, side, price, shares, amount, profit, trigger_note, condition_id]
    )
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id


@db_write
def execute_buy(code, name, shares, price, trigger_note=""):
    """买入原子操作：扣钱 → 更新持仓 → 写成交"""
    amt = round(price * shares, 2)
    cash = get_cash()
    if amt > cash:
        return False
    trade_id = add_trade(code, name, "buy", price, shares, amt, None, trigger_note)
    update_cash(-amt, "buy", trade_id)

    pos = get_position(code)
    if pos:
        ts = pos["shares"] + shares
        tc = pos["avg_cost"] * pos["shares"] + amt
        new_cost = round(tc / ts, 2)
        upsert_position(code, name, ts, new_cost)
    else:
        upsert_position(code, name, shares, round(price, 2))
    return True


@db_write
def execute_sell(code, shares, price, trigger_note=""):
    """卖出原子操作：检查持仓 → 写成交(含盈亏) → 加钱 → 更新持仓"""
    pos = get_position(code)
    if not pos or pos["shares"] < shares:
        return False
    rev = round(price * shares, 2)
    cf = pos["avg_cost"] * shares
    profit = round(rev - cf, 2)

    trade_id = add_trade(code, pos["name"], "sell", price, shares, rev, profit, trigger_note)
    update_cash(rev, "sell", trade_id)

    remaining = pos["shares"] - shares
    if remaining <= 0:
        upsert_position(code, pos["name"], 0, 0)
        remove_position_if_zero(code)
    else:
        upsert_position(code, pos["name"], remaining, pos["avg_cost"])
    return True


# ── 条件单读取/写入 ──

def load_conditions():
    """返回条件单dict列表, internal_state已解析"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM conditions WHERE account_id=1 AND status='active' ORDER BY id"
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["params"] = json.loads(d.get("params", "{}"))
        d["internal_state"] = json.loads(d.get("internal_state", "{}"))
        # 展开params到顶层（兼容条件引擎的字段引用）
        for k, v in d["params"].items():
            d[k] = v
        for k, v in d["internal_state"].items():
            d[k] = v
        # 保留兼容字段
        d["id"] = d["cid"]
        d["trigger"] = d["params"].get("trigger", "gte")
        d["trigger_price"] = d["params"].get("trigger_price", 0)
        d["created"] = d.get("created_at", "")
        result.append(d)
    return result


@db_write
def save_condition_status(cid, status, triggered_at=None, trigger_price=None, internal_state=None, note=None):
    """更新条件单状态 + internal_state"""
    conn = get_conn()
    sets = ["status=?", "updated_at=datetime('now','localtime')"]
    vals = [status]
    if triggered_at:
        sets.append("triggered_at=?")
        vals.append(triggered_at)
    if trigger_price is not None:
        sets.append("trigger_price=?")
        vals.append(trigger_price)
    if internal_state is not None:
        sets.append("internal_state=?")
        vals.append(json.dumps(internal_state))
    if note is not None:
        sets.append("note=?")
        vals.append(note)
    vals.append(cid)
    conn.execute(f"UPDATE conditions SET {', '.join(sets)} WHERE cid=?", vals)
    conn.commit()
    conn.close()


@db_write
def save_condition_state(cid, internal_state):
    """仅更新internal_state（条件引擎中间状态变化）"""
    conn = get_conn()
    conn.execute("UPDATE conditions SET internal_state=?, updated_at=datetime('now','localtime') WHERE cid=?",
                 [json.dumps(internal_state), cid])
    conn.commit()
    conn.close()


# ── 价格引擎 ──

class PriceEngine:
    def __init__(self):
        self._prices = {}
        self._source = "tencent"
        self._lock = threading.Lock()

    def _ts(self, code):
        return f"sh{code}" if code.startswith(("6", "5", "9")) else f"sz{code}"

    def _qs(self, code):
        return f"SHSE.{code}" if code.startswith(("6", "5")) else f"SZSE.{code}"

    def fetch_tencent(self, codes):
        try:
            syms = ",".join(self._ts(c) for c in codes)
            req = Request(f"{TENCENT_API}{syms}", headers={"User-Agent": "Mozilla/5.0"})
            r = urlopen(req, timeout=5)
            try:
                text = r.read().decode("gbk")
            except UnicodeDecodeError:
                text = r.read().decode("utf-8", errors="replace")
            results = {}
            for line in text.strip().split("\n"):
                if "=" not in line: continue
                vals = line.split("~")
                if len(vals) < 50: continue
                try:
                    bid_prices = [float(vals[11+i*2]) if vals[11+i*2] else 0 for i in range(5)]
                    bid_vols = [int(vals[10+i*2]) if vals[10+i*2] else 0 for i in range(5)]
                    ask_prices = [float(vals[21+i*2]) if vals[21+i*2] else 0 for i in range(5)]
                    ask_vols = [int(vals[20+i*2]) if vals[20+i*2] else 0 for i in range(5)]
                    results[vals[2]] = {
                        "price": float(vals[3]), "open": float(vals[5]) if vals[5] else 0,
                        "high": float(vals[33]) if vals[33] else 0, "low": float(vals[34]) if vals[34] else 0,
                        "pre_close": float(vals[4]) if vals[4] else 0,
                        "volume_lots": int(vals[6]) if vals[6] else 0,
                        "buy1": float(vals[11]) if vals[11] else 0,
                        "sell1": float(vals[21]) if vals[21] else 0,
                        "up_limit": float(vals[41]) if len(vals) > 41 and vals[41] else 0,
                        "down_limit": float(vals[42]) if len(vals) > 42 and vals[42] else 0,
                        # 新增丰富字段
                        "outer_disc": int(vals[7]) if vals[7] else 0,       # 外盘(主动买)
                        "inner_disc": int(vals[8]) if vals[8] else 0,       # 内盘(主动卖)
                        "change": float(vals[31]) if vals[31] else 0,       # 涨跌额
                        "change_pct": float(vals[32]) if vals[32] else 0,   # 涨跌幅%
                        "turnover_rate": float(vals[38]) if vals[38] else 0, # 换手率%
                        "pe": float(vals[39]) if vals[39] else 0,           # 市盈率
                        "volume_ratio": float(vals[43]) if vals[43] else 0,  # 量比
                        "amplitude": float(vals[46]) if vals[46] else 0,    # 振幅%
                        "market_cap": float(vals[48]) if len(vals) > 48 and vals[48] else 0,  # 总市值
                        "pb": float(vals[49]) if len(vals) > 49 and vals[49] else 0,          # 市净率
                        # 五档数组
                        "bid_prices": bid_prices, "bid_vols": bid_vols,
                        "ask_prices": ask_prices, "ask_vols": ask_vols,
                    }
                except (ValueError, IndexError): continue
            return results
        except Exception: return {}

    def fetch_qmt(self, code):
        try:
            r = urlopen(f"{QMT_BASE}/quote?symbol={self._qs(code)}", timeout=5)
            d = json.loads(r.read())
            p = d.get("price")
            if p and p > 0:
                return {code: {"price": p, "high": d.get("high",0), "low": d.get("low",0), "open": d.get("open",0)}}
        except: pass
        return {}

    def refresh_all(self, codes):
        if not codes: return
        now = time.time()
        self._source = "tencent"
        q2 = self.fetch_tencent(list(codes))
        if q2:
            with self._lock:
                for c, d in q2.items():
                    self._prices[c] = d | {"time": now}
            return
        self._source = "qmt"
        for code in codes:
            q3 = self.fetch_qmt(code)
            if q3:
                with self._lock:
                    self._prices[code] = q3[code] | {"time": now}

    def get_kline(self, code, count=300):
        """K线数据 — eltdx已禁用，请使用因子引擎"""
        return None

    def get_quote(self, code):
        with self._lock:
            q = self._prices.get(code)
            if q and time.time() - q.get("time",0) < 10:
                return q
        r = self.fetch_tencent([code])
        if r and code in r:
            with self._lock:
                self._prices[code] = r[code] | {"time": time.time()}
            return self._prices[code]
        return {}

    def refresh_loop(self, codes):
        while True:
            t0 = time.time()
            self.refresh_all(codes)
            e = time.time() - t0
            if e < REFRESH_SEC: time.sleep(REFRESH_SEC - e)


# ── 条件引擎 ──

def check_conditions_loop(pe, watched_codes):
    """条件检查主循环，每秒检查一次，DB读写"""
    while True:
        time.sleep(0.5)
        try:
            conditions = load_conditions()  # 仅active
            if not conditions: continue
            now = datetime.now()
            now_ts = now.strftime("%H:%M")
            today_str = now.strftime("%Y-%m-%d")
            changed = False

            for cond in conditions:
                if cond["status"] != "active": continue
                vu = cond.get("valid_until")
                if vu and vu < today_str:
                    save_condition_status(cond["cid"], "expired")
                    continue

                code = cond["code"]
                # 跳过正在被分析的条件
                if cond.get("_analyzing"):
                    continue
                quote = pe.get_quote(code)
                if not quote: continue
                cp = quote.get("price", 0)
                if not cp: continue

                ctype = cond.get("type", "price_trigger") or "price_trigger"
                # call_ai_ 前缀：条件触发后不交易，呼叫AI
                call_ai = False
                if ctype.startswith("call_ai_"):
                    call_ai = True
                    ctype = ctype[len("call_ai_"):]
                state = cond.get("_state", "idle")
                internal = dict(cond.get("internal_state", {}))
                internal["_state"] = state

                # ─── 定价买卖 ───
                if ctype == "price_trigger":
                    tp = cond.get("trigger_price", 0)
                    trig = cond.get("trigger", "gte")
                    hit = (trig == "lt" and cp < tp) or (trig == "gt" and cp > tp) or \
                          (trig == "lte" and cp <= tp) or (trig == "gte" and cp >= tp)
                    if hit:
                        if call_ai:
                            # 先执行条件单本身的交易
                            trade_ok = False
                            if cond["side"] == "sell" and pos_shares(code) > 0:
                                s = min(cond["shares"], pos_shares(code))
                                trade_ok = execute_sell(code, s, cp, cond.get("note",""))
                            elif cond["side"] == "buy":
                                trade_ok = execute_buy(code, cond.get("name", code), cond["shares"], cp, cond.get("note",""))
                            save_condition_status(cond["cid"], "triggered",
                                now.strftime("%H:%M:%S"), cp)
                            if trade_ok:
                                _call_ai_after_trade(cond, code, cp, now, quote, internal, True)
                            changed = True
                        elif cond["side"] == "sell" and pos_shares(code) > 0:
                            s = min(cond["shares"], pos_shares(code))
                            if execute_sell(code, s, cp, cond.get("note","")):
                                save_condition_status(cond["cid"], "triggered",
                                    now.strftime("%H:%M:%S"), cp)
                                changed = True
                        elif cond["side"] == "buy":
                            if execute_buy(code, cond.get("name", code), cond["shares"], cp, cond.get("note","")):
                                save_condition_status(cond["cid"], "triggered",
                                    now.strftime("%H:%M:%S"), cp)
                                changed = True

                # ─── 反弹买入 ───
                elif ctype == "bounce_buy":
                    mp = cond.get("monitor_price", 0)
                    rb = cond.get("rebound_pct", 2)
                    if state == "idle":
                        if cp <= mp:
                            internal["_state"] = "monitoring"
                            save_condition_state(cond["cid"], internal)
                    elif state == "monitoring":
                        if cp > mp:
                            internal["_state"] = "idle"
                            save_condition_state(cond["cid"], internal)
                        else:
                            internal["_lowest"] = min(internal.get("_lowest", cp), cp)
                            internal["_state"] = "watched"
                            save_condition_state(cond["cid"], internal)
                    elif state == "watched":
                        low = internal.get("_lowest", cp)
                        rebound = (cp - low) / low * 100
                        if rebound >= rb:
                            if execute_buy(code, cond.get("name", code), cond["shares"], cp, f"反弹{rebound:.1f}%"):
                                save_condition_status(cond["cid"], "triggered",
                                    now.strftime("%H:%M:%S"), cp,
                                    internal, f"最低{low:.2f}反弹{rebound:.1f}%")
                                if call_ai:
                                    _call_ai_after_trade(cond, code, cp, now, quote, internal, True)
                                changed = True

                # ─── 回落卖出 ───
                elif ctype == "pullback_sell":
                    mp = cond.get("monitor_price", 0)
                    pb = cond.get("pullback_pct", 3)
                    if state == "idle":
                        if cp >= mp:
                            internal["_state"] = "monitoring"
                            save_condition_state(cond["cid"], internal)
                    elif state == "monitoring":
                        if cp < mp:
                            internal["_state"] = "idle"
                            save_condition_state(cond["cid"], internal)
                        else:
                            internal["_peak"] = max(internal.get("_peak", cp), cp)
                            internal["_state"] = "watched"
                            save_condition_state(cond["cid"], internal)
                    elif state == "watched":
                        peak = internal.get("_peak", cp)
                        pullback = (peak - cp) / peak * 100
                        if pullback >= pb:
                            avail = pos_shares(code)
                            s = min(cond["shares"], avail)
                            if execute_sell(code, s, cp, f"回落{pullback:.1f}%"):
                                save_condition_status(cond["cid"], "triggered",
                                    now.strftime("%H:%M:%S"), cp,
                                    internal, f"最高{peak:.2f}回落{pullback:.1f}%")
                                if call_ai:
                                    _call_ai_after_trade(cond, code, cp, now, quote, internal, True)
                                changed = True

                # ─── 开板卖出 ───
                elif ctype == "opened_sell":
                    fb = cond.get("fallback_pct", 3)
                    up_limit = quote.get("up_limit", 0)
                    if not up_limit: continue
                    if state == "idle":
                        if cp >= up_limit * 0.995:
                            internal["_state"] = "opened"
                            internal["_peak"] = cp
                            save_condition_state(cond["cid"], internal)
                    elif state == "opened":
                        if cp < up_limit * 0.995:
                            internal["_state"] = "falling"
                            internal["_peak"] = max(internal.get("_peak", cp), cp)
                            save_condition_state(cond["cid"], internal)
                    elif state == "falling":
                        peak = internal.get("_peak", cp)
                        fb_actual = (peak - cp) / peak * 100
                        if fb_actual >= fb:
                            avail = pos_shares(code)
                            s = min(cond["shares"], avail)
                            if execute_sell(code, s, cp, f"开板回落{fb_actual:.1f}%"):
                                save_condition_status(cond["cid"], "triggered",
                                    now.strftime("%H:%M:%S"), cp,
                                    internal)
                                if call_ai:
                                    _call_ai_after_trade(cond, code, cp, now, quote, internal, True)
                                changed = True
                        elif cp >= peak:
                            internal["_state"] = "opened"
                            save_condition_state(cond["cid"], internal)

                # ─── 网格交易 ───
                elif ctype == "grid":
                    gl = cond.get("grid_low", 0)
                    gh = cond.get("grid_high", 0)
                    gs = cond.get("grid_step", 2)
                    gms = cond.get("grid_max_shares", 0)
                    if not gl or not gh: continue
                    price_range = gh - gl
                    grid_count = max(1, int(price_range / (gl * gs / 100)))
                    grid_size = price_range / grid_count
                    current_grid = int((cp - gl) / grid_size) if cp > gl else 0
                    current_grid = min(current_grid, grid_count)
                    last_grid = internal.get("_last_grid", -1)
                    if last_grid == -1:
                        internal["_last_grid"] = current_grid
                        save_condition_state(cond["cid"], internal)
                        continue
                    if current_grid < last_grid:
                        shares_per = max(100, gms // grid_count) if gms else 100
                        shares_per = (shares_per // 100) * 100
                        if execute_buy(code, cond.get("name", code), shares_per, cp, f"网格{last_grid}→{current_grid}"):
                            internal["_last_grid"] = current_grid
                            save_condition_state(cond["cid"], internal)
                            if call_ai:
                                _call_ai_after_trade(cond, code, cp, now, quote, internal, True)
                            changed = True
                    elif current_grid > last_grid:
                        shares_per = max(100, gms // grid_count) if gms else 100
                        shares_per = (shares_per // 100) * 100
                        avail = pos_shares(code)
                        if shares_per <= avail:
                            if execute_sell(code, shares_per, cp, f"网格{last_grid}→{current_grid}"):
                                internal["_last_grid"] = current_grid
                                save_condition_state(cond["cid"], internal)
                                if call_ai:
                                    _call_ai_after_trade(cond, code, cp, now, quote, internal, True)
                                changed = True

                # ─── 持仓回本 ───
                elif ctype == "breakeven_sell":
                    if not has_position(code): continue
                    cost = pos_cost(code)
                    if cp >= cost:
                        s = min(cond["shares"], pos_shares(code))
                        if execute_sell(code, s, cp, "回本"):
                            save_condition_status(cond["cid"], "triggered", now.strftime("%H:%M:%S"), cp)
                            if call_ai:
                                _call_ai_after_trade(cond, code, cp, now, quote, internal, True)
                            changed = True

                # ─── 时间触发 ───
                elif ctype == "time_trigger":
                    tt = cond.get("trigger_time", "")
                    if tt and now_ts >= tt:
                        price_check = cond.get("trigger_price", 0)
                        side_ok = (cond["side"] == "sell" and cp >= price_check) or \
                                  (cond["side"] == "buy" and cp <= price_check) or \
                                  (price_check == 0)
                        if side_ok:
                            if cond["side"] == "sell":
                                s = min(cond["shares"], pos_shares(code))
                                if execute_sell(code, s, cp, f"定时{tt}"):
                                    save_condition_status(cond["cid"], "triggered", now.strftime("%H:%M:%S"), cp)
                                    if call_ai:
                                        _call_ai_after_trade(cond, code, cp, now, quote, internal, True)
                                    changed = True
                            else:
                                if execute_buy(code, cond.get("name", code), cond["shares"], cp, f"定时{tt}"):
                                    save_condition_status(cond["cid"], "triggered", now.strftime("%H:%M:%S"), cp)
                                    if call_ai:
                                        _call_ai_after_trade(cond, code, cp, now, quote, internal, True)
                                    changed = True

                # ─── 做T ───
                elif ctype == "t_trade":
                    t_type = cond.get("t_type", "buy_first")
                    spread = cond.get("t_spread", 1.0)
                    batch = cond.get("shares", 100)
                    if t_type == "buy_first":
                        if not has_position(code):
                            if execute_buy(code, cond.get("name", code), batch, cp, "做T买入"):
                                if call_ai:
                                    _call_ai_after_trade(cond, code, cp, now, quote, internal, True)
                                changed = True
                        else:
                            cost = pos_cost(code)
                            profit_pct = (cp - cost) / cost * 100
                            if profit_pct >= spread:
                                s2 = min(pos_shares(code), batch)
                                if execute_sell(code, s2, cp, f"做T卖{profit_pct:.1f}%"):
                                    save_condition_status(cond["cid"], "triggered", now.strftime("%H:%M:%S"), cp)
                                    if call_ai:
                                        _call_ai_after_trade(cond, code, cp, now, quote, internal, True)
                                    changed = True
                    elif t_type == "sell_first":
                        if has_position(code) and pos_shares(code) >= batch:
                            if execute_sell(code, batch, cp, "做T先卖"):
                                if call_ai:
                                    _call_ai_after_trade(cond, code, cp, now, quote, internal, True)
                                changed = True
                        else:
                            last_sell = internal.get("_last_sell_price", 0)
                            if last_sell > 0:
                                drop = (last_sell - cp) / last_sell * 100
                                if drop >= spread:
                                    if execute_buy(code, cond.get("name", code), batch, cp, f"做T买回{drop:.1f}%"):
                                        internal["_last_sell_price"] = 0
                                        save_condition_status(cond["cid"], "triggered", now.strftime("%H:%M:%S"), cp, internal)
                                        if call_ai:
                                            _call_ai_after_trade(cond, code, cp, now, quote, internal, True)
                                        changed = True

            # 条件触发后更新监控列表
            if changed:
                pass  # 下次循环自动重新加载conditions

        except Exception as e:
            import traceback
            with open("/tmp/cond_loop_err.log", "a") as f:
                f.write(f"[{datetime.now()}] {type(e).__name__}: {e}\n{traceback.format_exc()}\n")


# ── HTTP API ──

pe = PriceEngine()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _json(self, d, s=200):
        self.send_response(s)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(d, ensure_ascii=False).encode())
    def _body(self):
        l = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(l)) if l else {}
    def _parse_code(self, raw):
        raw = raw.strip()
        if raw.startswith(("sh","sz","SH","SZ")):
            return raw[2:]
        return raw

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path.strip("/")
        if path == "status":
            cash = get_cash()
            positions = get_positions_dict()
            total = cash
            ps = []
            for c, p in positions.items():
                mv = round(p["shares"] * p["avg_cost"], 2)
                total += mv
                ps.append({"code": c, "name": p["name"], "shares": p["shares"],
                           "avg_cost": p["avg_cost"], "market_value": mv})
            conn = get_conn()
            total_trades = conn.execute("SELECT COUNT(*) FROM trades WHERE account_id=1").fetchone()[0]
            conds = conn.execute("SELECT status FROM conditions WHERE account_id=1").fetchall()
            conn.close()
            active = sum(1 for c in conds if c["status"] == "active")
            self._json({"cash": cash, "positions": ps, "price_source": pe._source,
                        "total_trades": total_trades, "total_value": round(total, 2),
                        "conditions_total": len(conds), "conditions_active": active})

        elif path == "trades":
            conn = get_conn()
            rows = conn.execute("SELECT * FROM trades WHERE account_id=1 ORDER BY id").fetchall()
            conn.close()
            out = []
            for r in rows:
                d = dict(r)
                d["side"] = d["side"].upper() + (f"({d['trigger_note']})" if d["trigger_note"] else "")
                d["time"] = d.pop("created_at")
                out.append(d)
            self._json({"trades": out})

        elif path == "conditions":
            conn = get_conn()
            rows = conn.execute("SELECT * FROM conditions WHERE account_id=1 ORDER BY id").fetchall()
            conn.close()
            out = []
            for r in rows:
                d = dict(r)
                # 展开params中常用字段（兼容）
                try:
                    params = json.loads(d.get("params", "{}"))
                except: params = {}
                for k in ("trigger", "trigger_price", "monitor_price", "rebound_pct",
                          "pullback_pct", "fallback_pct", "grid_low", "grid_high",
                          "grid_step", "grid_max_shares", "trigger_time", "t_type", "t_spread"):
                    if k in params:
                        d[k] = params[k]
                d["id"] = d.pop("cid")
                d["created"] = d.pop("created_at")
                # internal_state -> 展开_chain_id供前端显示链状态
                try:
                    istate = json.loads(d.pop("internal_state", "{}"))
                except:
                    istate = {}
                if istate:
                    d["internal_state"] = istate
                d.pop("account_id", None)
                out.append(d)
            self._json({"conditions": out})

        elif path.startswith("ai-stats"):
            conn = get_conn()
            # 支持 ai-stats/detail/xxx 取单条链的逐条详情
            if path.startswith("ai-stats/detail/"):
                target_chain = path.split("ai-stats/detail/")[1]
                rows = conn.execute("""
                    SELECT sequence, trigger_cid, code, price, action, is_trade,
                           analysis, llm_response, response_time, created_at
                    FROM ai_stats WHERE chain_id=? ORDER BY sequence
                """, [target_chain]).fetchall()
                conn.close()
                details = []
                for r in rows:
                    details.append({
                        "sequence": r["sequence"],
                        "trigger_cid": r["trigger_cid"],
                        "code": r["code"],
                        "price": r["price"],
                        "action": r["action"],
                        "is_trade": r["is_trade"],
                        "analysis": r["analysis"],
                        "llm_response": r["llm_response"],
                        "response_time": r["response_time"],
                        "created_at": r["created_at"],
                    })
                self._json({"chain_id": target_chain, "details": details})
                return

            rows = conn.execute("""
                SELECT chain_id, code, 
                       COUNT(*) AS calls,
                       SUM(is_trade) AS trades,
                       ROUND(AVG(response_time), 2) AS avg_resp_time,
                       GROUP_CONCAT(action) AS actions,
                       MIN(created_at) AS first_call,
                       MAX(created_at) AS last_call
                FROM ai_stats GROUP BY chain_id ORDER BY last_call DESC LIMIT 20
            """).fetchall()
            conn.close()
            out = []
            for r in rows:
                actions = (r["actions"] or "").split(",")
                waits = sum(1 for a in actions if "wait" in a)
                skips = sum(1 for a in actions if "skip" in a)
                total = len(actions)
                friction = round((skips + waits) / total * 100, 1) if total else 0
                out.append({
                    "chain_id": r["chain_id"], "code": r["code"],
                    "calls": r["calls"], "trades": r["trades"] or 0,
                    "chain_length": total,
                    "friction_pct": friction,
                    "avg_resp_time": r["avg_resp_time"],
                    "first_call": r["first_call"], "last_call": r["last_call"],
                })
            self._json({"chains": out})

        elif path == "restrictions":
            self._json(TRADING_RESTRICTIONS)

        elif path == "fund":
            conn = get_conn()
            cash = conn.execute("SELECT cash FROM accounts WHERE id=1").fetchone()[0]
            total_deposits = conn.execute(
                "SELECT COALESCE(SUM(delta), 0) FROM balance_logs WHERE reason='deposit' AND account_id=1"
            ).fetchone()[0]
            total_withdrawals = conn.execute(
                "SELECT COALESCE(SUM(delta), 0) FROM balance_logs WHERE reason='withdraw' AND account_id=1"
            ).fetchone()[0]
            conn.close()
            self._json({"cash": cash, "total_deposits": total_deposits, "total_withdrawals": abs(total_withdrawals)})

        elif path.startswith("kline/"):
            code = self._parse_code(path.split("kline/", 1)[1])
            count = int(self.path.split("count=")[1].split("&")[0]) if "count=" in self.path else 300
            q = pe.get_kline(code, count)
            if q:
                self._json({"code": code, "source": pe._source, "bars": q})
            else:
                self._json({"error": "unavailable"}, 504)

        elif path.startswith("quote/"):
            code = self._parse_code(path.split("quote/", 1)[1])
            q = pe.get_quote(code)
            if q and q.get("price"):
                self._json({"price": q["price"], "source": pe._source, "code": code,
                           "high": q.get("high"), "low": q.get("low"),
                           "open": q.get("open"), "pre_close": q.get("pre_close"),
                           "volume_lots": q.get("volume_lots"),
                           "change_pct": q.get("change_pct"), "turnover_rate": q.get("turnover_rate"),
                           "volume_ratio": q.get("volume_ratio"), "amplitude": q.get("amplitude"),
                           "outer_disc": q.get("outer_disc"), "inner_disc": q.get("inner_disc"),
                           "pe": q.get("pe"), "pb": q.get("pb"),
                           "market_cap": q.get("market_cap"),
                           "bid_prices": q.get("bid_prices"), "bid_vols": q.get("bid_vols"),
                           "ask_prices": q.get("ask_prices"), "ask_vols": q.get("ask_vols"),
                           "up_limit": q.get("up_limit"), "down_limit": q.get("down_limit")})
            else:
                self._json({"error": "unavailable"}, 504)

        elif path.startswith("factor/"):
            code = self._parse_code(path.split("factor/", 1)[1])
            factor_text = _get_full_factor(code)
            self._json({"code": code, "factor": factor_text})

        else:
            self._json({"error": "?"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path.strip("/")

        if path == "trade":
            try:
                b = self._body()
                code = self._parse_code(b["code"])
                side = b["side"].strip().lower()
                price = float(b["price"])
                shares = int(b["shares"])
                name = b.get("name", f"股票{code}")
                if side == "buy":
                    if not execute_buy(code, name, shares, price, "手动"):
                        return self._json({"error": "资金不足"}, 400)
                    msg = f"买入 {name}({code}) {shares}股 @{price:.2f}"
                else:
                    if not execute_sell(code, shares, price, "手动"):
                        return self._json({"error": "无持仓或股数不足"}, 400)
                    msg = f"卖出 {name}({code}) {shares}股 @{price:.2f}"
                self._json({"status": "ok", "message": msg, "cash": get_cash(), "positions": get_positions_dict()})
            except Exception as e:
                self._json({"error": str(e)}, 500)

        elif path == "conditions":
            try:
                b = self._body()
                code = self._parse_code(b["code"])
                cid = b.get("id", f"cond-{int(time.time())}")
                ctype = b.get("type", "price_trigger")
                side = b.get("side", "sell")
                shares = int(b.get("shares", 100))
                note = b.get("note", "")
                valid_until = b.get("valid_until", "")
                name = b.get("name", f"股票{code}")

                # 自动加入AI链：如果该标的存在活跃的call_ai_条件，新条件也转为call_ai_
                # 这样手动/API创建的条件也能参与链式交易，不会断链
                istate = {}
                if not ctype.startswith("call_ai_"):
                    try:
                        conn_chk = get_conn()
                        existing = conn_chk.execute(
                            "SELECT COUNT(*) FROM conditions WHERE account_id=1 AND code=? AND status='active' AND type LIKE 'call_ai_%'",
                            [code]
                        ).fetchone()[0]
                        conn_chk.close()
                        if existing > 0:
                            ctype = "call_ai_" + ctype
                            istate = {"_chain_id": cid, "_seq": 1, "_origin": "user",
                                      "_origin_note": note, "_constraint": parse_constraints(note)}
                    except:
                        pass

                # 构建params JSON（支持call_ai_前缀，解析后自动剥离）
                base_type = ctype.replace("call_ai_", "", 1) if ctype.startswith("call_ai_") else ctype
                params = {"trigger": b.get("trigger", "gte")}
                max_pos_pct = b.get("max_position_pct", None)
                if max_pos_pct is not None:
                    params["max_position_pct"] = float(max_pos_pct)
                # 交易限制参数（逐条可配）
                for rkey in ["allow_batch","batch_max_count","allow_batch_sell",
                             "price_slippage_pct","stop_loss_default_pct","round_lot",
                             "min_cash_reserve_pct"]:
                    if rkey in b:
                        params[rkey] = b[rkey]
                if base_type == "price_trigger":
                    params["trigger"] = b.get("trigger", "gte")
                    params["trigger_price"] = float(b.get("trigger_price", 0))
                elif base_type in ("bounce_buy",):
                    params["monitor_price"] = float(b.get("monitor_price", 0))
                    params["rebound_pct"] = float(b.get("rebound_pct", 2))
                elif base_type in ("pullback_sell",):
                    params["monitor_price"] = float(b.get("monitor_price", 0))
                    params["pullback_pct"] = float(b.get("pullback_pct", 3))
                elif base_type == "opened_sell":
                    params["fallback_pct"] = float(b.get("fallback_pct", 3))
                elif base_type == "grid":
                    params["grid_low"] = float(b.get("grid_low", 0))
                    params["grid_high"] = float(b.get("grid_high", 0))
                    params["grid_step"] = float(b.get("grid_step", 2))
                    params["grid_max_shares"] = int(b.get("grid_max_shares", 0))
                elif base_type == "time_trigger":
                    params["trigger_time"] = b.get("trigger_time", "")
                    params["trigger_price"] = float(b.get("trigger_price", 0))
                elif base_type == "t_trade":
                    params["t_type"] = b.get("t_type", "buy_first")
                    params["t_spread"] = float(b.get("t_spread", 1.0))
                elif base_type == "breakeven_sell":
                    pass

                conn = get_conn()
                with _db_write_lock:
                    if istate:
                        conn.execute(
                            "INSERT INTO conditions (account_id, cid, code, name, type, side, status, params, shares, valid_until, note, internal_state) "
                            "VALUES (1,?,?,?,?,?,'active',?,?,?,?,?)",
                            [cid, code, name, ctype, side, json.dumps(params), shares,
                             valid_until or None, note, json.dumps(istate)]
                        )
                    else:
                        conn.execute(
                            "INSERT INTO conditions (account_id, cid, code, name, type, side, status, params, shares, valid_until, note) "
                            "VALUES (1,?,?,?,?,?,'active',?,?,?,?)",
                            [cid, code, name, ctype, side, json.dumps(params), shares,
                             valid_until or None, note]
                        )
                    conn.commit()
                conn.close()

                self._json({"status": "ok", "condition": {
                    "id": cid, "type": ctype, "code": code, "side": side,
                    "status": "active", "note": note, "shares": shares
                }})
            except Exception as e:
                self._json({"error": str(e)}, 500)

        elif path == "reset":
            # 重置仿真 — 清空并重新初始化
            with _db_write_lock:
                conn = get_conn()
                conn.execute("DELETE FROM conditions WHERE account_id=1")
                conn.execute("DELETE FROM trades WHERE account_id=1")
                conn.execute("DELETE FROM positions WHERE account_id=1")
                conn.execute("DELETE FROM balance_logs WHERE account_id=1")
                conn.execute("UPDATE accounts SET cash=?, updated_at=datetime('now','localtime') WHERE id=1", [DEFAULT_CASH])
                conn.execute("INSERT INTO positions (account_id, code, name, shares, avg_cost) VALUES (1,'601138','工业富联',850,65.32)")
                conn.execute("INSERT INTO balance_logs (account_id, delta, balance_after, reason) VALUES (1,?,?,'reset')",
                             [DEFAULT_CASH, DEFAULT_CASH])
                conn.commit()
                conn.close()
            self._json({"status": "ok", "cash": DEFAULT_CASH, "positions": {"601138": {"name": "工业富联", "shares": 850, "avg_cost": 65.32}}})

        elif path.startswith("conditions/") and path.endswith("/manual"):
            try:
                cid = path.split("conditions/", 1)[1].split("/manual")[0]
                with _db_write_lock:
                    conn = get_conn()
                    r = conn.execute("SELECT type FROM conditions WHERE account_id=1 AND cid=? AND status='active'", [cid]).fetchone()
                    if not r:
                        conn.close()
                        return self._json({"error": "条件不存在"}, 404)
                    ctype = r["type"]
                    if not ctype.startswith("call_ai_"):
                        conn.close()
                        return self._json({"error": "已经是普通条件单，无需取消AI"}, 400)
                    new_type = ctype.replace("call_ai_", "", 1)
                    conn.execute("UPDATE conditions SET type=?, internal_state='{}', updated_at=datetime('now','localtime') WHERE cid=? AND account_id=1",
                                 [new_type, cid])
                    conn.commit()
                    conn.close()
                self._json({"status": "ok", "message": f"AI已退出，条件类型 {ctype} → {new_type}"})
            except Exception as e:
                self._json({"error": str(e)}, 500)


        elif path == "fund":
            try:
                b = self._body()
                amount = float(b.get("amount", 0))
                conn = get_conn()
                cash = conn.execute("SELECT cash FROM accounts WHERE id=1").fetchone()[0]
                
                if amount > 0:
                    # 追加
                    new_cash = cash + amount
                    conn.execute("UPDATE accounts SET cash=?, updated_at=datetime('now','localtime') WHERE id=1", [new_cash])
                    conn.execute("INSERT INTO balance_logs (account_id, delta, balance_after, reason) VALUES (1,?,?,'deposit')",
                                 [amount, new_cash])
                elif amount < 0:
                    # 取出
                    withdraw = abs(amount)
                    total_value = cash + sum(
                        p["shares"] * p["avg_cost"] for p in get_positions_dict().values()
                    )
                    min_reserve_cash = total_value * TRADING_RESTRICTIONS["min_cash_reserve_pct"] / 100
                    max_withdraw = cash - min_reserve_cash
                    if withdraw > max_withdraw:
                        conn.close()
                        return self._json({
                            "error": f"最多可取¥{max_withdraw:,.0f}（现金{cash:,.0f}-保底{TRADING_RESTRICTIONS['min_cash_reserve_pct']}%×总资产=¥{min_reserve_cash:,.0f}）",
                            "max_withdraw": max_withdraw
                        }, 400)
                    new_cash = cash - withdraw
                    conn.execute("UPDATE accounts SET cash=?, updated_at=datetime('now','localtime') WHERE id=1", [new_cash])
                    conn.execute("INSERT INTO balance_logs (account_id, delta, balance_after, reason) VALUES (1,?,?,'withdraw')",
                                 [-withdraw, new_cash])
                else:
                    conn.close()
                    return self._json({"error": "amount must be != 0"}, 400)
                
                conn.commit()
                conn.close()
                self._json({"status": "ok", "cash": new_cash, "amount": amount, 
                           "total_deposits": new_cash if amount > 0 else cash})
            except Exception as e:
                # ensure conn is closed on error
                try: conn.close()
                except: pass
                self._json({"error": str(e)}, 500)

        elif path == "restrictions":
            try:
                b = self._body()
                for k, v in b.items():
                    if k in TRADING_RESTRICTIONS:
                        TRADING_RESTRICTIONS[k] = v
                self._json({"status": "ok", "restrictions": TRADING_RESTRICTIONS})
            except Exception as e:
                self._json({"error": str(e)}, 500)

        elif path.startswith("conditions/") and path.endswith("/analyze"):
            cid = path.split("conditions/")[1].split("/analyze")[0]
            try:
                conn = get_conn()
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM conditions WHERE account_id=1 AND cid=?", [cid]).fetchone()
                conn.close()
                if not row:
                    return self._json({"error": "not found"}, 404)
                # 先标记为分析中，防止条件检查循环重复处理
                with _db_write_lock:
                    conn2 = get_conn()
                    conn2.execute("UPDATE conditions SET internal_state='{\"_analyzing\":true}', updated_at=datetime('now','localtime') WHERE account_id=1 AND cid=? AND status='active'",
                                 [cid])
                    conn2.commit()
                    conn2.close()
                cond = dict(row)
                cond["params"] = json.loads(cond.get("params", "{}"))
                cond["internal_state"] = json.loads(cond.get("internal_state", "{}"))
                for k, v in cond["params"].items():
                    cond[k] = v
                code = cond["code"]
                now = datetime.now()
                quote = pe.get_quote(code)
                cp = quote.get("price", 0) if quote else 0
                if not cp:
                    return self._json({"error": "无法获取行情"}, 504)
                # 手动分析：在上下文中标明这不是触发事件
                user_msg = "## ⚠️ 这是一次手动分析请求，条件尚未触发\n请基于当前持仓和行情给出建议，**不要**执行实际交易。\n\n" + build_ai_context(cond, code, cp, now.strftime("%Y-%m-%d %H:%M:%S"), quote)
                messages = [
                    {"role": "system", "content": AI_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ]
                ai_t0 = time.time()
                ai_resp = call_llm(messages)
                llm_time = round(time.time() - ai_t0, 3)
                chain_id = cond.get("internal_state", {}).get("_chain_id") or cid
                seq = cond.get("internal_state", {}).get("_seq", 1)

                # 首次执行
                ok = execute_ai_decision(ai_resp, cid, now, cp, chain_id, seq, llm_time)
                if not ok:
                    # 检查AI是否返回了request_factor
                    try:
                        import re as _re
                        _m = _re.search(r'"action"\s*:\s*"request_factor"', ai_resp)
                        if _m:
                            # 补因子数据重叫AI
                            factor_text = (_get_full_factor(code) or "（暂无）")
                            if factor_text and len(factor_text) > 10:
                                user_msg2 = user_msg + "\n\n## 完整因子数据\n" + factor_text + "\n\n请基于以上完整因子数据重新决策。"
                                ai_resp2 = call_llm([{"role":"system","content":AI_SYSTEM_PROMPT},{"role":"user","content":user_msg2}])
                                ok = execute_ai_decision(ai_resp2, cid, now, cp, chain_id, seq, 0)
                    except: pass
                # 手动分析 → 不改变条件状态（不标记triggered）
                # 且不写入888.txt（避免混淆为自动触发）
                # 只需移除分析标记
                with _db_write_lock:
                    conn3 = get_conn()
                    conn3.execute("UPDATE conditions SET internal_state='{}', updated_at=datetime('now','localtime') WHERE account_id=1 AND cid=? AND status='active'",
                                 [cid])
                    conn3.commit()
                    conn3.close()
                self._json({"status": "ok" if ok else "error", "ai_response": ai_resp[:500]})
            except Exception as e:
                err = str(e)
                # database is locked → 重试1次（给条件循环让路）
                if ("locked" in err.lower() or "busy" in err.lower()):
                    import time as _t; _t.sleep(3)
                    try:
                        # 重试整个流程：重新取行情
                        quote2 = pe.get_quote(code)
                        cp2 = quote2.get("price", 0) if quote2 else 0
                        if cp2:
                            user_msg2 = "## ⚠️ 手动分析（重试）\n\n" + build_ai_context(cond, code, cp2, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), quote2)
                            ai_resp2 = call_llm([{"role":"system","content":AI_SYSTEM_PROMPT},{"role":"user","content":user_msg2}])
                            ok2 = execute_ai_decision(ai_resp2, cid, datetime.now(), cp2, chain_id, seq, 0)
                            with _db_write_lock:
                                conn_r = get_conn()
                                conn_r.execute("UPDATE conditions SET internal_state='{}' WHERE account_id=1 AND cid=? AND status='active'", [cid])
                                conn_r.commit(); conn_r.close()
                            self._json({"status":"ok" if ok2 else "error", "ai_response": ai_resp2[:500]})
                            return
                    except:
                        pass
                self._json({"error": err}, 500)

        else:
            self._json({"error": "?"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path.strip("/")
        if path.startswith("conditions/"):
            cid = path.split("conditions/", 1)[1]
            with _db_write_lock:
                conn = get_conn()
                conn.execute("UPDATE conditions SET status='cancelled', updated_at=datetime('now','localtime') WHERE account_id=1 AND cid=?",
                             [cid])
                conn.commit()
                conn.close()
            self._json({"status": "ok", "action": "cancelled"})
        else:
            self._json({"error": "?"}, 404)


def main():
    print(f"\n🧪 仿真交易 v8 — SQLite + 10种条件单", flush=True)
    db_init()
    print(f"  数据库: {DB_PATH}", flush=True)
    print(f"  端口: {HOST}:{PORT}", flush=True)
    print(f"  条件单: 定价买卖/止盈止损/反弹买入/回落卖出/开板卖出/网格交易/持仓回本/时间触发/做T", flush=True)

    cash = get_cash()
    positions = get_positions_dict()
    acts = get_conn().execute(
        "SELECT COUNT(*) FROM conditions WHERE account_id=1 AND status='active'"
    ).fetchone()[0]
    get_conn().close()
    print(f"  持仓{len(positions)}只, 现金{cash:,.0f}", flush=True)
    print(f"  条件单: {acts}条活跃", flush=True)

    watched = set()
    for c in positions: watched.add(c)
    conn = get_conn()
    for r in conn.execute("SELECT DISTINCT code FROM conditions WHERE account_id=1 AND status='active'"):
        watched.add(r["code"])
    conn.close()
    if not watched: watched.add("601138")
    print(f"  监控: {len(watched)}只 {sorted(watched)}", flush=True)

    t = threading.Thread(target=pe.refresh_loop, args=(list(watched),), daemon=True)
    t.start()
    t2 = threading.Thread(target=check_conditions_loop, args=(pe, list(watched)), daemon=True)
    t2.start()
    print(f"  📡 价格刷新+条件检查: 已启动", flush=True)

    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    print(f"  ✅ 就绪 ({HOST}:{PORT})", flush=True)
    class ReuseServer(HTTPServer):
        allow_reuse_address = True
    ReuseServer((HOST, PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
