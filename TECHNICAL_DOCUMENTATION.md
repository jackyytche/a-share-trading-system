# A股交易系统 — 技术文档

> 最后更新: 2026-07-10
> 涵盖: 网络拓扑、数据源、仿真引擎v8(SQLite+链式AIv2)、因子引擎(93列完整)、知识库、链式AI策略交易、AI链条件清理规则

---

## 一、项目概览

Hermes Agent 驱动的A股量化交易系统，运行于 NAS Docker 双容器环境（Gateway + WebUI）。覆盖数据采集、因子计算、选股策略、仿真交易、链式AI决策全链路。

---

## 二、网络拓扑

```
┌─ 互联网 ─────────────────────────────────────────┐
│  腾讯行情API(qt.gtimg.cn / proxy.finance.qq.com)  │
│  东财datacenter HTTP / 妙想MCP API                │
│  DeepSeek API / DashScope API                     │
└──────────────────────────────────────────────────┘
           ↑ 家宽(CGNAT)
┌── 主路由 192.168.2.1 ────────────────────────────┐
│  ISP拨号 + DHCP                                  │
└──────────────────────────────────────────────────┘
           ↑
┌── 旁路由 iStoreOS 192.168.2.2 ───────────────────┐
│  OpenClash，国内IP自动bypass不代理                │
│  root/helegr4m                                    │
│  SOCKS5:7891  HTTP:7890  Dashboard:9090          │
└──────────────────────────────────────────────────┘
           ↑ 网关/DNS指向192.168.2.2
┌── NAS fnOS 192.168.2.254 ────────────────────────┐
│  admin/helegr4m                                   │
│  CPU: Athlon II X4 640 (无SSE4.1/AVX)            │
│                                                    │
│  Gateway容器 (cron调度器/消息推送)                  │
│  ├─ ~/.hermes/scripts/ (共享卷)                   │
│  ├─ ~/.hermes/config.yaml                         │
│  ├─ 有 /home/hermes/.hermes/                      │
│  └─ 无 /workspace 挂载                           │
│                                                    │
│  WebUI容器 (交互/DB操作/daemon)                     │
│  ├─ /workspace/ (bind mount)                      │
│  ├─ simulation_daemon.py :7408                   │
│  ├─ factor_viewer_api.py :7410                   │
│  ├─ 因子引擎 /workspace/factor_engine/ (93列)     │
│  ├─ simulation.db                                │
│  └─ factor_store.db (唯一物理文件，零副本)         │
│                                                    │
│  iptables DNAT: 192.168.2.254:7410 → 172.19.0.4  │
└──────────────────────────────────────────────────┘

Windows 192.168.2.68:7403 — QMT网关(报价+K线+筛股)
Windows 192.168.2.220 — 旧IP(已迁移到2.68)
```

---

## 三、数据源架构

### 优先级链（2026-07-09更新）

```
① 腾讯行情API  — qt.gtimg.cn / proxy.finance.qq.com，HTTP免费无限，88字段含五档盘口[10-29]
   2026-07-09: K线API从 ifzq.gtimg.cn 切换至 proxy.finance.qq.com（更稳定）
   2026-07-09: 修复 count=1→68(日线)/256(周线) bug（导致EMA9/RSI/MACD全空的根因）
   并发提升: max_workers=1→10（HTTP API无连接限制）

② eltdx TCP    — ❌ 2026-07-09起永久禁用。12台服务器全被封，不等待解封
   所有代码中eltdx引用改为惰性导入，无eltdx时自动降级腾讯API或DB读取

③ QMT网关      — Windows端(192.168.2.68:7403)，213ms，稳定降级

④ 东财datacenter — EPS/营收/ROE，HTTP直连
   妙想MCP (mxapi.eastmoney.com) — 11个工具，300分/月配额，深度财务查询

⑤ 量价代理      — flow.py从K线量价比算资金流（push2替代）
```

### 腾讯API 88字段发现（2026-07-08）

长期被当做末位保底，实际含以下关键字段：

| 字段索引 | 内容 | 用途 |
|:--------|:-----|:-----|
| [3] | 当前价 | 实时行情 |
| [4] | 昨收 | 涨跌幅基准 |
| [5] | 今开 | 开盘价 |
| [6] | 成交量(手) | 量能 |
| [7][8] | 外盘/内盘 | 买卖力度 |
| [9-18] | 买1~5量价 | 五档盘口 |
| [19-28] | 卖1~5量价 | 五档盘口 |
| [33][34] | 最高/最低 | 日振幅 |
| [38] | 换手率 | 活跃度 |
| [39] | 市盈率(动态) | 估值 |
| [41][42] | 涨停/跌停价 | 涨跌停 |
| [43] | 量比 | 放量程度 |
| [46] | 振幅 | 波动率 |
| [48] | 总市值(亿) | 市值 |
| [49] | 市净率 | 估值 |

K线API: `https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param=sh{code},day,,,68,qfq`

详见 `simulation-daemon` skill → `references/tencent-api-fields.md`。

### eltdx 铁律（2026-07-09最终版）

- **eltdx已永久禁用**。所有服务器被封，不等待解封
- `fetch_fundamentals_all.py` / `run_all_market.py` 的 eltdx 导入改为惰性加载
- 无eltdx时降级腾讯API或DB读取
- `ELTDX_ALLOWED` 环境变量仍保留但仅用于遗留代码兼容
- sitecustomize.py 全局阻断保留

### 各数据源能力

| 能力 | eltdx | QMT网关 | 腾讯API | 东财API | 妙想MCP |
|:----|:-----|:-------|:-------|:--------|:--------|
| 实时行情+五档 | ❌永久禁 | ✅ | ✅ | ❌ | ❌ |
| PE/PB/市值 | ❌ | 需计算 | ✅直接给 | ❌ | ✅ |
| K线(proxy/250根) | ❌ | ✅(30根) | ✅(68根) | ❌ | ❌ |
| 集合竞价 | ❌ | ❌ | ❌ | ❌ | ❌ |
| 财务数据 | ❌ | ⚠️弃用 | ❌ | ✅ | ✅ |
| 股息率 | ❌ | ❌ | ❌ | ❌ | ❌ |
| 资金流向 | ❌ | ⚠️推2封 | ❌ | ❌ | ❌ |
| EPS/营收 | ❌ | ❌ | ❌ | ✅ | ✅ |
| 智能选股/公告 | ❌ | ❌ | ❌ | ❌ | ✅ |

### 妙想MCP状态（2026-07-09验证）

- **initialize握手：** ✅ 正常（protocolVersion 2025-11-25）
- **tools/list：** ✅ 11个工具可用（mx_ashare_finance_data / mx_fund_finance_data 等）
- **调用规则：** 简单行情→腾讯API（免费无限），深度财务/选股→妙想MCP（300分/月）

### 推2(push2)封锁

2026-07-01起push2.eastmoney.com因CGNAT IP封锁不可达，Docker和Windows均受影响。资金流向改用K线量价代理（`flow.py` → `_proxy_from_kline()`）。2026-07-09验证仍不可达。

---

## 四、仿真交易引擎 v8

### 概述

独立守护进程，WebUI容器内运行，不依赖Hermes会话生命周期。

### 文件位置

| 文件 | 说明 |
|:----|:------|
| `/workspace/simulation_daemon.py` | 主程序 v8 (SQLite+链式AI) |
| `/workspace/simulation.db` | SQLite数据库(7张表) |
| `/workspace/call_ai_form.html` | 链式交易Web界面 |
| `/workspace/factor_viewer_api.py` | 因子浏览器+反向代理(:7410) |
| `/workspace/sim_watchdog.py` | 服务watchdog(含因子引擎每日更新触发) |
| `/workspace/migrate_to_sqlite.py` | v7→v8数据迁移脚本 |

### 数据库7表

| 表 | 用途 | 说明 |
|:----|:-----|:------|
| accounts | 账户资金 | WAL事务保护 |
| balance_logs | 资金变更流水 | INSERT ONLY，审计追踪 |
| positions | 当前持仓 | upsert模式，shares=0自动清理 |
| trades | 成交记录 | INSERT ONLY，永不修改 |
| conditions | 条件单 | status流转: active→triggered/expired/cancelled |
| ai_stats | AI决策统计 | 每次链式AI决策记录 |
| price_log | 价格快照 | 可选，默认关 |

### DB锁修复（2026-07-09）

**问题：** "分析"按钮报"database is locked"。三处连接 `factor_store.db` 未设 WAL 模式导致并发读写锁。

**修复：**
- `factor_viewer_api.py` → `get_conn()`: `timeout=20` + `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=5000`
- `simulation_daemon.py` → `_get_factor_ref()`: `timeout=10` + `PRAGMA journal_mode=WAL`
- `simulation_daemon.py` → `_get_full_factor()`: `timeout=10` + `PRAGMA journal_mode=WAL`

### API (:7408 + :7410反向代理)

前端页面 `call_ai_form.html` 通过 `factor_viewer_api.py` (:7410) 的 `/daemon/` 反向代理访问 daemon。外部访问入口：`http://192.168.2.254:7410/call_ai`（需NAS侧iptables DNAT）。

| 方法 | 路径 | 说明 |
|:----|:----|:------|
| GET | /status | 持仓+资金+价源+条件单统计 |
| GET | /trades | 成交记录 |
| GET | /conditions | 条件单列表(params展开到顶层,含internal_state) |
| POST | /conditions | 设置条件单(支持call_ai_前缀+逐条交易限制) |
| DELETE | /conditions/:id | 撤销条件单(status=cancelled) |
| POST | /conditions/:id/analyze | 手动触发AI分析 |
| POST | /conditions/:id/manual | AI退出，保留条件单为普通price_trigger |
| GET | /quote/:code | 实时行情(含五档盘口+实时估值) |
| GET | /kline/:code | K线数据(默认300根,?count=N) |
| POST | /trade | 手动执行交易 |
| POST | /reset | 重置仿真仓 |
| GET | /restrictions | 查看交易限制配置 |
| POST | /restrictions | 修改交易限制 |
| GET | /fund | 查看资金池 |
| POST | /fund | 追加资金 |
| GET | /ai-stats | 链式交易质量报告(ℱ摩擦系数) |

### 条件单类型（8种call_ai_版）

所有条件单通过前端 `call_ai_form.html` 递交时自动转为 `call_ai_` 前缀。触发时**先执行交易，再唤醒AI**。

| 类型 | 说明 | 关键参数 |
|:----|:-----|:--------|
| `call_ai_price_trigger` | 定价交易 | trigger=lte/gte, trigger_price |
| `call_ai_bounce_buy` | 先跌到监控价→再反弹买入 | monitor_price, rebound_pct |
| `call_ai_pullback_sell` | 先涨到监控价→再回落卖出 | monitor_price, pullback_pct |
| `call_ai_opened_sell` | 涨停打开回落卖 | fallback_pct |
| `call_ai_grid` | 区间网格高抛低吸 | grid_low/high/step/max_shares |
| `call_ai_breakeven_sell` | 回本就卖 | 无 |
| `call_ai_time_trigger` | 定时+价条件交易 | trigger_time, trigger_price |
| `call_ai_t_trade` | 做T | t_type, t_spread |

### 链式AI策略交易（2026-07-08重构v2）

**核心变更：** 从"触发→仅叫AI→AI决定要不要执行"改为**"触发→先执行条件单本身的交易→再叫AI做后续决策"**。

```
daemon 5s轮询（0 token消耗）
  │  条件触发（call_ai_）
  ▼
┌── 价格敏感冷却 ──────────────────┐
│ 全局_ai_cooling按code存储        │
│ 价格变动<0.5% 且 距上次<120秒 →  │
│   跳过AI，保持条件active          │
│ 否则 → 更新冷却记录，继续          │
└─────────────────────────────────┘
  ▼
① execute_sell/execute_buy → 条件单本身的交易 ✅
  │  持仓已更新、现金已变动
  ▼
② call_llm(AI_SYSTEM_PROMPT + 最新持仓/现金/五档盘口)
  │  JSON 返回 (3种action)
  ▼
execute_ai_decision():
  ├─ place_conditions → INSERT新call_ai_条件
  │   note=origin_note（用户原始策略，永不被覆盖）
  ├─ wait → 跳过，等已有条件
  └─ skip → AI看到空仓或决定退出，链结束
```

### 用户原始策略永不丢失

AI下新条件时通过 `chain_id` 追溯到链源头条件单的 `note`，存入新条件的 `internal_state._origin_note`，并覆盖 `note` 字段。链中每一环AI看到的 `备注` 都是用户最初的策略指令。

```python
# execute_ai_decision()中
origin_note = ""
if chain_id:
    r2 = conn.execute("SELECT note FROM conditions WHERE cid=?", [chain_id]).fetchone()
    if r2 and r2[0]:
        origin_note = r2[0][:200]
note = origin_note or ai_note  # 原始策略优先
```

### 价格敏感冷却（2026-07-08新增）

```python
# 全局dict，按标的code存储（跨条件单生效）
_ai_cooling[code] = {"price": cp, "time": now_s}

# 判断条件
pct_move < 0.5%  AND  time_since < 120s  →  跳过AI
```

冷却只阻止AI调用，**不影响交易执行**。用于防止价格在359.90~360.00微幅震荡时反复唤AI（之前吉比特14轮1分半钟的教训）。

### `_call_ai_after_trade()` helper

统一所有8种类型的冷却+调AI逻辑，避免代码重复：

```python
def _call_ai_after_trade(cond, code, cp, now, quote, internal, trade_ok=True):
    # 冷却检查
    # build_ai_context → call_llm → execute_ai_decision
```

### 链式交易质量评估 (ai_stats)

**Agentic Friction ℱ：** AI质疑自身的程度
```
ℱ = (skip + wait) / 总调用次数 × 100%
ℱ越高→AI越谨慎  ℱ越低→AI越果断
```

**链长 L：** 从首次call_ai到真实交易的决策轮数

**查询接口：** `curl http://127.0.0.1:7408/ai-stats`

### AI链条件清理规则（2026-07-10）

**规则：** 同类型的AI条件单只有最新的生效，用户的第一张条件单不受影响。

AI每轮通过 `execute_ai_decision()` 的 `place_conditions` 分支下新条件前，自动执行清理：

```python
# 伪逻辑
for 每种(类型, 方向) in 本轮新条件:
    UPDATE conditions SET status='cancelled'
    WHERE code=? AND status='active'
    AND type LIKE 'call_ai_{类型}%' AND side=?
    AND cid != 当前触发条件  # 保留已触发的
    AND cid != 链首ID       # 保留用户原始条件
```

**清理范围：**
| 要取消的 | 保留的 |
|:---------|:-------|
| 同标的、同类型+同方向的旧 `call_ai_` 条件 | 用户原始链首条件（`chain_id`） |
| 过期未执行的条件 | 刚触发的条件（`trigger_cid`） |
| 上一轮AI产生的同策略条件 | 不同类型的条件（如本轮只下止损，旧止盈不受影响） |

**触发示例：**
```
旧条件: 止盈@30 (call_ai_price_trigger sell) + 止损@26.5 (call_ai_price_trigger sell)
本轮AI: 触发止盈→下止损@27.5 (call_ai_price_trigger sell)
→ 清理: 旧止损@26.5（同类型+同方向）→ cancelled
→ 保留: 旧止盈@30（已triggered）、用户链首条件
→ 新建: 止损@27.5
```

### 手动条件自动加入AI链（2026-07-10）

通过 `POST /conditions` 创建条件时，如果该标的存在活跃的 `call_ai_` 条件，自动将新条件转为 `call_ai_` 前缀并写入链标记：

```python
istate = {"_chain_id": cid, "_seq": 1, "_origin": "user", "_origin_note": note}
ctype = "call_ai_" + ctype
```

这确保手动/API创建的条件也能参与链式交易，触发后自动唤醒AI做后续决策，不会断链。

**存量转换（2026-07-10）：**
- `603444-sell-390` → `call_ai_price_trigger sell @390`
- `601138-stop-60-v3` → `call_ai_price_trigger sell lte@60`

### 数据库连接三件套（2026-07-10）

所有SQLite连接统一使用以下配置，防止 `database is locked`：

```python
conn = sqlite3.connect(DB_PATH, timeout=N)     # 等待N秒
conn.execute("PRAGMA journal_mode=WAL")          # 读写不阻塞
conn.execute("PRAGMA busy_timeout=5000")         # 锁冲突等5秒
```

受影响的函数：`get_conn()`、`_get_factor_ref()`、`_get_full_factor()`

---

## 五、因子引擎

### 概述

全A股多因子计算系统，每日收盘后自动更新。SQLite存储，93列含MSCI六因子评分。**2026-07-09数据源重构**：eltdx永久禁用，K线API切换至proxy.finance.qq.com。

### DB位置

```
/workspace/factor_engine/factor_store.db  ← 唯一物理文件
（2026-07-09曾迁至~/.hermes/factor_engine/，已恢复。零副本零软链。）
```

### 文件结构

```
/workspace/factor_engine/
├── __init__.py              # 包初始化
├── runner.py                # 关注池更新器(含资金流代理)
├── run_all_market.py        # 全市场更新(Phase1+Phase2)
│   ├── proxy.finance.qq.com  # K线API（2026-07-09切换）
│   ├── count=68(日线)/256(周线) # EMA9/RSI修复
│   ├── max_workers=10        # 并发提升
│   ├── DB fallback           # eltdx不通时读DB
│   └── eltdx惰性导入         # 导入失败自动降级
├── fetcher.py               # 数据获取(单例TdxClient，已惰性化)
├── store.py                 # SQLite存储(93列)
├── fill_missing_factors.py  # 补缺(PCF/营收增速/MSCI)
│   ├── phase2_pcf           # PCF（东财API直连）
│   ├── phase3_growth        # 营收增速（东财API独立通道）
│   └── phase4_msci          # MSCI重算
├── fetch_fundamentals_all.py # 基本面批量拉取（eltdx导入已惰性化）
│   ├── _get_tdx() → 无eltdx时返回None
│   └── _try_get_ltx_data() → 降级腾讯API补基本面
├── factor_profile.py        # 因子暴露画像工具
├── factors/
│   ├── __init__.py           # 因子注册表
│   ├── technical.py          # 技术因子(MA/RSI/MACD/布林/ATR)
│   ├── momentum.py           # 动量因子
│   ├── fundamental.py        # 基本面(PE/PEG)
│   ├── flow.py               # 资金流向(推2→量价代理自动降级)
│   ├── signal.py             # 信号因子
│   ├── score.py              # 综合评分(左侧/右侧)
│   ├── msci.py               # MSCI六因子评分
│   └── financial_health.py   # 财务健康因子
├── factor_store.db           # SQLite数据库(93列)
├── factor_viewer_api.py      # :7410 HTTP服务器(因子浏览+链式页面)
└── factor_profile.html       # 因子画像HTML
```

### 当前覆盖（2026-07-09）

| 模块 | 覆盖 | 数据源 |
|:----|:----:|:-------|
| 价格/市值 | **5,206/5,206 (100%)** | 腾讯行情 |
| 技术因子(ema9/rsi6/macd) | **5,203/5,206 (99.9%)** | proxy K线(68日+256周) |
| MSCI六因子 | **5,206/5,206 (100%)** | factors/msci.py |
| 资金流向(代理) | **5,194/5,206 (99.8%)** | flow.py量价比代理 |
| PE/TTM EPS | **4,101/5,206 (78.8%)** | 东财API |
| 营收增速 | **1,693/5,206** | 东财API（Phase 3逐批填补） |
| DB大小 | **10.9 MB** | — |

### 93列字段分类

| 类别 | 字段数 | 来源 |
|:----|:-----:|:-----|
| 价格/市值 | 3 | 腾讯API |
| 技术因子 | 18 | proxy K线(原eltdx) |
| 动量因子 | 5 | proxy K线(原eltdx) |
| 波动率/风险 | 7 | 计算 |
| 估值 | 5 | 东财API |
| 财务 | 7 | 东财API |
| 增长 | 2 | 东财API |
| 资金流向 | 4 | 量价代理 |
| 信号/评分 | 16 | 计算 |
| MSCI六因子 | 7 | 计算 |
| 其他 | 19 | 综合 |

### Cron更新（2026-07-09重构）

Cron调度器运行在 **Gateway容器**，无 `/workspace` 挂载。因此需要 `/workspace` 的操作由 WebUI 侧组件执行：

| 时间(CST) | Gateway (cron) | WebUI (watchdog) |
|:----------|:--------------|:-----------------|
| 15:30 | `factor_daily_update.py`(纯SQL校验，0秒，无/workspace时跳过) | — |
| 15:25-40 | — | `sim_watchdog` → `run_all_market.py`(全量更新，300s超时) |
| 15:35 | `factor_phase3.py`(营收增速，独立脚本) | — |

**设计原则：** agent模式cron走LLM+工具（不依赖/workspace），no_agent脚本如需操作DB需在WebUI侧（watchdog/后台进程）。

| 参数 | 值 |
|:----|:----|
| 脚本(主) | `~/.hermes/scripts/factor_daily_update.py` |
| 脚本(Phase 3) | `~/.hermes/scripts/factor_phase3.py` |
| 模式 | no_agent=True (纯脚本) |
| 全量触发 | WebUI侧watchdog (sim_watchdog.py，15:25-40) |
| 增量逻辑 | 只处理当日尚无因子/失败重试的股票 |
| 耗时 | 基础更新~5s，全量(含PCF+腾讯补)~20s，营收增速(Phase3)~4-5分钟 |

### 常用命令

```bash
# 查因子
python3 /workspace/factors.py 601138
python3 /workspace/factors.py --screener top100
python3 /workspace/factors.py --screener "pb<1,roa>5,msci_momentum>50"

# 手动更新
python3 /workspace/factors.py --update           # 关注池
python3 /workspace/factor_engine/run_all_market.py  # 全市场

# 查因子引擎状态（浏览器）
curl http://127.0.0.1:7410/factor_viewer
```

---

## 六、eltdx硬阻断机制

2026-07-07起实施，2026-07-09升级为**永久禁用**。所有Python进程启动时通过 `sitecustomize.py` 自动替换 `eltdx.TdxClient` 为阻断版。代码中所有eltdx导入已惰性化，无eltdx时自动降级。

```
/usr/local/lib/python3.12/site-packages/sitecustomize.py
                  ↓
Python启动→替换TdxClient→检查ELTDX_ALLOWED环境变量
                  ↓
      true? → 放行（仅遗留代码兼容）
      false? → 报错："eltdx直连被阻断！走daemon HTTP API"
```

2026-07-09新增：所有引用eltdx的模块改为惰性导入，`_get_tdx()` 在 `ImportError` 时返回 `None`，调用方自行降级。

---

## 七、知识库系统

| 组件 | 技术 | 说明 |
|:----|:----|:------|
| 存储 | SQLite | `/workspace/data/trading_kb.db` |
| 向量 | 1536维 float数组 | DashScope text-embedding-v2 |
| 全文搜索 | SQLite FTS5 | kb_fts 虚拟表 |
| CLI | `/workspace/data/kb.py` | 增删改查 |

---

## 八、Skills 清单

| skill | 类别 | 说明 |
|:------|:----|:------|
| `simulation-daemon` | trading | 仿真引擎v8(SQLite+AI链式+WAL锁修复) |
| `factor-engine` | trading | 因子引擎93列+全市场更新+proxy K线 |
| `a-share-trading-pitfalls` | trading | 踩坑记录(含eltdx永久禁止) |
| `hermes-trading-decision` | trading | 实盘交易决策流程 |
| `etf-trend` | trading | ETF趋势跟踪系统 |
| `msci-first` | trading | MSCI优先法则 |
| `market-event-mapping` | trading | 事件→股票映射 |
| `eastmoney-analysis` | trading | 东财分析技能(含妙想MCP) |
| `hermes-etf-signal` | trading | ETF信号生成器 |
| `a-share-stock-screening` | trading | A股基本面选股 |
| `knowledge-base-rag` | data-science | 知识库RAG架构 |
| `home-network-topology` | networking | 家庭网络拓扑 |

---

## 九、QMT网关 (Windows)

运行于 Windows 192.168.2.68:7403（原2.220已迁移）。

| 端点 | 说明 |
|:----|:------|
| /quote | 实时行情 |
| /kline | K线数据 |
| /screener | 全市场筛股 |
| /concept-flow | 概念板块资金流向(推2封禁→空) |
| /signal | 发送信号到QMT策略 |
| /save-report | 保存文件(base64 JSON) |

文件传输：POST 192.168.2.68:7403/save-report JSON `{content: base64}` → 保存到 `C:\\e\\`

---

## 十、重要配置

### 端口占用

| 端口 | 服务 | 说明 |
|:----|:-----|:------|
| 7403 | QMT网关 | Windows 192.168.2.68 |
| 7408 | 仿真引擎v8 | WebUI容器 |
| 7410 | 因子浏览器+链式页面 | WebUI容器（需iptables DNAT外部访问） |
| 8787 | Hermes WebUI | WebUI容器 |
| 8645 | Hermes LLM代理 | 本地(若启用) |

### 7410端口外部访问

**iptables DNAT（临时，容器重启后丢失）：**
```bash
sudo iptables -t nat -I DOCKER 1 ! -i docker0 -p tcp --dport 7410 -j DNAT --to-destination 172.19.0.4:7410
sudo iptables -I FORWARD 1 -p tcp -d 172.19.0.4 --dport 7410 -j ACCEPT
```

**持久化脚本：** `/usr/local/bin/fix-7410.sh`（自动发现容器IP+iptables转发）
**crontab：** `@reboot sleep 30 && /usr/local/bin/fix-7410.sh`

### 数据库一览

| 数据库 | 路径 | 类型 | 访问方 |
|:------|:----|:----|:-------|
| `factor_store.db` | `/workspace/factor_engine/` | ✅ 物理文件 | WebUI（gateway不访问） |
| `simulation.db` | `/workspace/` | ✅ 物理文件 | WebUI daemon |
| `trading_kb.db` | `/workspace/data/` | ✅ 物理文件 | WebUI |
| `state.db` | `~/.hermes/` | ✅ 物理文件 | 两边共享 |

所有数据库零副本、零软链。

### 时区

所有时间使用北京时间(CST=UTC+8)。daemon内部设 `os.environ["TZ"]="Asia/Shanghai"` + `time.tzset()`。

---

## 十一、持仓状态（2026-07-09收盘）

| 标的 | 股数 | 成本 | 市值 |
|:----|:---:|:----:|:----:|
| 002345 潮宏基 | 1,000 | 10.50 | ~10,500 |
| 603444 吉比特 | 400 | 361.38 | ~144,552 |
| 601138 工业富联 | 1,000 | 66.01 | ~66,010 |
| 600030 中信证券 | 2,000 | 28.25 | ~56,500 |
| 现金 | — | — | 634,756 |
| **合计** | | | **~912,318** |

上周持仓的300059东方财富（2000股）已通过链式AI全部清仓。潮宏基今日卖出8000股@9.80（亏5,600），留存1000股。601138工业富联重新建仓1000股@66.01。

---

## 十二、开发注意事项

### 容器架构约束（2026-07-09新增）
1. **Gateway容器无/workspace** — 操作DB的脚本不能做no_agent cron，需WebUI侧watchdog或agent模式
2. **agent模式cron正常** — 走LLM调用web_search等工具，不受/workspace限制
3. **共享卷唯一入口** — 脚本统一放 `~/.hermes/scripts/`，两边容器各自路径访问
4. `/home/hermes` symlink已删除（无用，两个容器各自有独立的路径入口）

### eltdx 铁律（最重要）
1. **eltdx已永久禁用**，不等待解封
2. 所有代码中eltdx引用已惰性导入，不安装也不报错
3. 无eltdx时自动降级腾讯API或DB读取
4. sitecustomize.py 全局阻断保留（遗留代码兼容）

### 因子引擎更新
1. K线已切 proxy.finance.qq.com，ifzq.gtimg.cn 废弃
2. count=68(日线)/256(周线) 是EMA9/RSI/MACD计算的必要条件
3. 营收增速走东财datacenter独立通道（Phase 3独立cron）
4. 全量更新由WebUI侧watchdog在15:25-40窗口触发（300s超时）
5. DB已迁移回 `/workspace/factor_engine/`，零副本零软链

### DB锁（2026-07-09新增）
factor_store.db被多个进程访问（daemon + viewer），所有连接必须设 `PRAGMA journal_mode=WAL` + `timeout>=10`。当前三处已修复，新增代码记得加。

### 链式AI交易（2026-07-08重构v2）
1. AI从config.yaml读取凭证，换模型改配置即可
2. 系统prompt在代码`AI_SYSTEM_PROMPT`常量中（8种call_ai_类型表）
3. **核心逻辑变更：** call_ai触发→先执行条件单交易→冷却检查→再叫AI
4. **用户原始策略永不丢失：** AI下条件时note=origin_note，链中每环都看到用户原始指令
5. **价格敏感冷却：** 全局按code存储，变动<0.5%且<120秒跳过AI
6. ai_stats表记录每次AI决策，`/ai-stats`接口查看质量报告
7. 日志写入`/workspace/888.txt`

### 被删除的废弃脚本
- `start_sse.py`（旧SSE桥，多连接被封的原因之一）
- `strategy_monitor.py`（旧策略监控）
- `trade_loop.py`（旧交易循环）
- `/home/hermes` symlink（2026-07-09删除，无用）

### NAS CPU限制
Athlon II X4 640 缺SSE4.1/AVX：纯Python计算，无numpy/pyarrow。

---

## 十三、链式AI交易测试执行结果（2026-07-08）

### 执行概况

| 项目 | 结果 |
|:----|:----:|
| 测试标的 | 300059东方财富（买入2000@20.51→清仓）、603444吉比特（14轮链式决策）、601138工业富联（清仓后重仓）、002345潮宏基（部分卖出）、600030中信证券（建仓） |
| 总AI调用 | 30+次 |
| 发现并修复的bug | ①`build_ai_context`缺少`quote`参数→条件检查循环静默崩溃②`except:pass`吞异常③买入强制call_ai_导致永不成交④note被AI覆盖→用户原始策略丢失⑤三处DB连接缺WAL→database is locked |
| 新增功能 | 先交易后唤醒AI、价格敏感冷却、note按链追溯、8类型全call_ai_支持、三处WAL锁修复 |

### 关键bug修复记录

| Bug | 根因 | 修复 |
|:----|:-----|:-----|
| 条件检查循环不触发 | `build_ai_context`引用未定义变量`quote`→`except:pass`吞异常 | 加`quote=None`参数+兜底+两处调用点传值 |
| 吉比特14轮循环不成交 | 买入条件强制call_ai_导致永不执行真实交易 | 改为"先交易后唤醒AI"，call_ai触发先execute再叫AI |
| AI链中丢失用户策略 | AI下条件时note被自己的analysis覆盖 | 追溯chain_id源头note→存入origin_note→覆盖note |
| 同标的快速反复唤AI | 每张新条件单在相同价格立即触发 | 全局按code冷却，变动<0.5%且<120秒跳过AI |
| database is locked | 三处factor_store.db连接未设WAL | 加PRAGMA WAL + busy_timeout(2026-07-09) |

### 最终验证

2026-07-08 19:22测试通过：call_ai_price_trigger sell 601138 1股→execute_sell ✅→AI被叫→ai_stats记录`is_trade=1`。2026-07-09 WAL修复后全链路验证通过。

---

## 十四、仿真引擎daemon自动重启方案

### 问题

`simulation_daemon.py` 是Hermes容器内的后台进程（端口7408）。重启Hermes容器时，所有容器内进程终止，daemon不会自动恢复。

### 双重保障架构

| 层面 | 方案 | 响应时间 | 原理 |
|:----|:----|:-------:|:-----|
| **A. NAS宿主** | fnOS crontab @reboot | NAS启动后~2分钟 | 脚本等容器就绪→`docker exec`启动 |
| **B. 容器内** | Hermes cron watchdog (每2分钟) | 容器重启后≤2分钟 | no_agent脚本检查7408/7410端口，挂则重启 |

### 方案A：NAS @reboot

**脚本位置：** `/home/admin/start_sim_daemon.sh` (fnOS NAS宿主机)

```
@reboot /bin/bash /home/admin/start_sim_daemon.sh > /tmp/sim_daemon_cron.log 2>&1
```

等待 `hermes-webui` 容器启动（最长120秒），然后执行：
```bash
docker exec -d hermes-webui bash -c "cd /workspace && python3 simulation_daemon.py"
```

### 方案B：Hermes cron watchdog

**cron job：** `仿真引擎v8 watchdog` (job_id: `e1a82e78cf00`)

| 参数 | 值 |
|:----|:----|
| 脚本 | `~/.hermes/scripts/sim_watchdog.py` |
| 频率 | 每2分钟 |
| 模式 | no_agent=True (零token消耗) |
| 行为 | 正常时静默；daemon挂→清理残留→重启→输出告警 |

2026-07-09新增：watchdog增加因子引擎全量更新触发（15:25-40 CST窗口）和 `cwd='/workspace'` 路径保护。

```python
# watchdog核心逻辑
s = socket.socket()
r = s.connect_ex(('127.0.0.1', 7408))
if r != 0:
    subprocess.Popen(['python3', path], cwd=wd)  # wd='/workspace'或None
# 15:25-40窗口触发全量更新
if _HOUR == 7 and 25 <= _MINUTE <= 40:
    subprocess.run(['python3', '/workspace/factor_engine/run_all_market.py'], timeout=300)
```

### 恢复能力

- **NAS整机重启** → A先触发（~2分钟），B二次保障（再等2分钟）
- **仅容器重启** → B在容器就绪后≤2分钟内恢复
- **daemon异常退出** → B在2分钟内检测到并拉起
- **数据库完整**：conditions/positions/trades/ai_stats 全部在 SQLite 中，重启后自动加载

---

## 十五、因子库数据质量与填补

### 字段填充率总览（2026-07-09）

| 类别 | 填充率 | 状态 |
|:----|:-----:|:----:|
| MSCI六因子 | 99.9% | ✅ 完整 |
| 均线系统（ma5/10/20/60/ema12/26） | 99.8% | ✅ |
| 技术指标（RSI/MACD/布林/ATR） | 99.8% | ✅ |
| **ema9/ema9_trend** | **99.9%** | **✅ 2026-07-09修复（count=1→68/256 bug）** |
| 估值（PE/PB） | 99% | ✅ |
| 入场/趋势评分 | 0.3% | ⏳ 需60日K线全量计算 |
| 财务健康（ROA/ROE/负债率） | 0-99% | 部分字段完整 |
| 资金流向（flow_1d/5d/20d） | 0% | ❌ push2被封 |
| 流动性评分 | 0% | ❌ 需多维度数据 |

### 已修复的空缺

#### ema9/ema9_trend（2026-07-09修复）
- **根因：** `run_all_market.py` 中腾讯API K线 URL 写死 `count=1`，腾讯只返回1根K线 → `len(days) < 30` 永远 False → EMA9/RSI/MACD等技术因子全空
- **修复：** `day,,,1` → `day,,,68`，`week,,,1` → `week,,,256`
- **连带修复：** `max_workers=1→10`，`get_full_stock_list()` 加DB降级
- **结果：** 5,206只带技术因子入库，EMA9缺失仅3只

#### boll_middle = ma20（布林中轨=20日均线）
- **根因：** `store.py` 第67行键名不匹配—— `boll` dict 写入时用 `"mid"`，store.py 读取时查 `"middle"`
- **修复：** `"middle"` → `"mid"`
- **填补：** SQL UPDATE `boll_middle = ma20`，10381行，0.4% → 99.8%

#### price_change
- **填补：** 从 `ret_1d` + `price` 推导：`price_change = price - price / (1 + ret_1d/100)`
- **结果：** 0% → 99.9%，10424行

### 需后续计算（数据就绪，函数待跑）

`compute_one_stock()` 已新增以下计算：

| 新增字段 | 依赖数据 |
|:---------|:---------|
| entry_score/level/detail | 60日K线（entry_timing_score）|
| trend_score/level/detail | 60日K线（trend_following_score）|
| val_score/level | PE/PB等已有估值字段 |
| vp_confirm | 日K线量价 |
| volume_change/amihud | 流动性因子 |

```bash
# 全量手动更新
python3 /workspace/factor_engine/run_all_market.py
```
