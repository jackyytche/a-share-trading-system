# 链式AI交易系统开发进度 — 2026-07-10

## ✅ 已完成

### Daemon后端
- [x] REFRESH_SEC 1.5s→5s（验证TDX限频）
- [x] DB写锁: Lock → RLock（可重入，防死锁）
- [x] @db_write 装饰器（序列化所有写入操作）
- [x] 分析前标记 `_analyzing=true`，条件检查循环跳过
- [x] 链式AI：买入强制 `call_ai_` 前缀，卖出留 `price_trigger`
- [x] AI下发的所有条件始终写 `_chain_id` + `_origin` + `_origin_note`
- [x] GET /conditions 返回 `internal_state` 对象
- [x] POST /conditions/:id/manual（AI退出，保留条件）
- [x] POST /conditions/:id/analyze（手动触发AI决策）
- [x] GET/POST /restrictions（交易限制配置）
- [x] GET/POST /fund（资金池追加/取出）
- [x] 留底金改为百分比 `min_cash_reserve_pct`
- [x] 取出时最大可取值: 现金 - 总资产 × 保底%
- [x] **腾讯API 88字段集成**：五档盘口[10-29]、外盘/内盘、换手率、量比、振幅、PE、PB、总市值
- [x] **AI上下文升级**：五档盘口 + 实时行情 + 实时估值 + 原始策略追溯(_origin_note)
- [x] **eltdx 15分钟冷却**：不通时每15分钟才重试一次
- [x] **妙想MCP恢复**：API key已配置回 config.yaml
- [x] **应收增速改用东财API**：phase3_growth不再依赖eltdx
- [x] **DAEMON + VIEWER DB锁修复**：`_get_factor_ref()` / `_get_full_factor()` / `get_conn()` 加 PRAGMA journal_mode=WAL + busy_timeout

### call_ai表单页面
- [x] 三Tab布局（新增/活跃/历史）
- [x] 新增Tab：8种策略类型联动参数
- [x] 新增Tab：逐条交易限制（分批/价偏离/止损/单位/留底%）
- [x] 新增Tab：预设方案（清仓出货/新建仓位/做T）
- [x] 股票搜索框（因子库5275只）
- [x] 提交后自动清空 + 跳到活跃 tab
- [x] 活跃Tab：状态标签（⏳等待 / 🔗 AI链）
- [x] 活跃Tab：操作按钮（分析/手动/取消/📋决策）
- [x] 活跃Tab：Tab2过滤增加 `ai-` 前缀条件
- [x] 提示词完整显示（取消截取40字）
- [x] Toast通知支持自定义时长
- [x] 顶部资金池/限制栏（追加/取出/编辑）

### 因子引擎数据源（2026-07-09 Alice session变更）
- [x] **K线API切换**：`ifzq.gtimg.cn` → `proxy.finance.qq.com`（更稳定）
- [x] **count修复**：日线 `count=1→68`，周线 `count=1→256`（EMA9/RSI/MACD数据为空的根因）
- [x] **并发提升**：`max_workers=1→10`（HTTP API无连接限制）
- [x] **全量更新完成**：5201只带EMA9/MA5/MACD/RSI/MSCI（99.9%），当前5206只
- [x] **eltdx永久禁用**：不可逆。所有代码中eltdx引用改为惰性导入，无eltdx时降级DB读取或腾讯API
- [x] **DB fallback**：`get_full_stock_list()` eltdx不通时从factor_store.db读现有列表
- [x] **Cron脚本重写**：纯SQLite实现，零外部依赖。gateway容器无/workspace时优雅跳过

### 端口转发与DB路径重构（2026-07-09）
- [x] **7410端口从NAS可访问**：iptables DNAT规则修正（`-d 172.19.0.4` → `! -i docker0`）
- [x] **factor_store.db 迁移回 /workspace**：共享卷副本已删除，唯一物理文件在workspace
- [x] **所有DB零副本**：factor_store.db / simulation.db / trading_kb.db 各自唯一位

### 2026-07-10 链式AI页面全面修复
- [x] **触发价列空白修复**：`c.params` JSON字符串未解析，改为 `JSON.parse()`
- [x] **NaN资金提示修复**：`d.min_cash_reserve` 旧字段已不存在，改用百分比 `min_cash_reserve_pct` 计算
- [x] **历史Tab按链分组 + 时间排序**：按 `_chain_id` 分组，可折叠展开，全局折叠/展开按钮
- [x] **双击提示词看完整AI上下文**：显示原始策略 + 条件参数 + AI链决策详情（从ai_stats表逐条取）
- [x] **活跃Tab按标的分组合并**：卡片式布局，标头显示持仓/成本/浮盈/已实现盈亏
- [x] **操作按钮移到卡片表头**：🔍分析链 / 🟡批量手动 / 🔴批量取消，不再每行重复
- [x] **每行保留状态标签+📋按钮**：状态标签 / 📋查看AI决策详情

### 2026-07-10 后端修复
- [x] **分析按钮不销毁条件单**：`POST /conditions/:id/analyze` 不再标记为triggered
- [x] **手动分析AI上下文**：开头标明"这是手动分析请求，条件尚未触发"，防AI幻觉
- [x] **request_factor循环处理**：AI要求看因子数据时，补全93列因子后重叫AI
- [x] **所有SQLite连接统一 busy_timeout=5000**：`_get_factor_ref`/`_get_full_factor`/`get_conn` 全补上
- [x] **factor_viewer_api代理超时提升**：GET 8s→45s、DELETE 8s→15s
- [x] **database locked自动重试**：捕获锁异常后sleep 3s重试一次
- [x] **同类型AI条件自动清理**：`execute_ai_decision()` 下新条件前，取消同标的同类型+同方向的旧AI条件
- [x] **保留用户原始链首**：排除 `chain_id`（用户第一张条件单）不被清理
- [x] **手动条件自动转call_ai_**：`POST /conditions` 时检测标的已有活跃AI条件，自动加入AI链
- [x] **手动条件存量转换**：`603444-sell-390`、`601138-stop-60-v3` 转为 `call_ai_price_trigger`

## 🐞 已知问题

### 高频
1. **分析时条件检查循环竞争** — ✅已修复（_analyzing标记+RLock）
2. **📋决策按钮查不到记录** — ✅已修复（传_chain_id而非c.id）
3. **AI下条件消失** — ✅已修复（internal_state始终写_chain_id）
4. **daemon重启后旧进程占端口** — SO_REUSEADDR已启用

### 待观察
5. **分析耗时太长** — call_llm 10-20s，toast改为45秒可见
6. **601138只剩1股** — 原因：条件单触发了卖出，残仓未补
7. **600030条件消失** — 可能是AI skip后无新条件
8. **eltdx仍被TDX限频** — 永久禁用，不等待解封
9. **gateway cron无/workspace** — 设计如此。操作DB的脚本改由WebUI侧watchdog执行

## 🔗 关键文件
- `/workspace/simulation_daemon.py` — daemon主程序
- `/workspace/call_ai_form.html` — 链式交易Web界面
- `/workspace/factor_viewer_api.py` — 因子浏览器+链式页面反向代理
- `/workspace/sim_watchdog.py` — 服务watchdog（含因子引擎每日更新触发）
- `~/.hermes/scripts/factor_daily_update.py` — cron校验脚本
- `~/.hermes/scripts/factor_phase3.py` — cron营收增速脚本
- `~/.hermes/scripts/chain_ai_report.py` — cron链式AI收盘报告

## 📝 待讨论（2026-07-10）

### ① 动态调整 vs 用户硬约束的矛盾

**现状：** `origin_note` 机制让AI永远看到用户原始策略提示词（如"止盈@30"），每轮AI决策都受这个硬约束。

**问题：** 如果标的的基本面/技术面发生变化（如利好爆发、业绩超预期），AI应该能动态上调止盈目标，而不是死守用户最初设定的30元。但当前 `origin_note` 强制覆盖AI每次下条件的 `note` 字段，AI即使想上调也做不到。

**具体场景：**
```
用户原始策略: "2000股@28.25，止盈30"
↓
股价涨到32，技术面MSCI动量90，基本面PE仍然合理
↓
AI看到origin_note"止盈30" → 只能继续下止盈@30
↓
错失上涨利润
```

**可能的解决方向：**
1. **过期机制：** `origin_note` 设一个有效期（如7天），过期后AI可以自由调整
2. **AI评分覆盖：** 当AI的因子分析达到某个置信度时，允许覆盖原始策略
3. **用户授权模式：** 在策略提示词里加标记，如 `[动态]` 表示允许AI自行调整阈值，`[固定]` 表示严格约束
4. **分层约束：** 用户设的是"策略方向"（买/卖/止盈/止损）而非"具体价格"，价格由AI根据因子数据决定

### ② 冷却阈值实盘验证（当前0.5%/120s）
### ③ 交易失败时AI感知（execute_sell/buy返回False后通知AI）
### ⑦ 收盘后（15:00+）阻止交易但允许AI做收盘分析

### ⑧ 网格+call_ai：每格都叫AI是否太频繁，需特殊处理

### ⑨ 做T+call_ai：阶段性交易（先买/先卖）不该触发完整AI链

---

## 🔭 Roadmap — 分层控制与审计（基于前沿研究，2026-07参考）

> 以下方向来自两篇arXiv论文的架构启发，不是当前开发计划。

### 参考论文1：Agentic Trading Survey

**《Agentic Trading: When LLM Agents Meet Financial Markets》** (arXiv:2605.19337, 2026-05)
深大团队综述，覆盖77篇论文。提出通用架构：

```
Perception → Memory/RAG → Reasoning → Execution
                                         ↓
                              Risk / Compliance Filters
                                         ↓
                              Logs / Audit / Human Oversight
```

与我们系统的映射：

| 论文层 | 我们已实现的 | 可演进方向 |
|:-------|:------------|:-----------|
| Risk/Compliance Filters | `[固定]`/`[区间]`/`[动态]` + 价格修正 | 数学验证层（见下方Lean 4） |
| Immutable Audit Trail | `ai_stats`表 | 决策前快照 + 执行后偏差分析 |
| Human Oversight | 链首条件保留 | AI建议→用户确认→执行的半自动模式 |

### 参考论文2：Lean 4 定理证明约束

**《Type-Checked Compliance》** (arXiv:2604.01483, 2026-04)
用 **Lean 4** 数学定理证明器做AI交易合规检查——**将"硬约束"从运行时检查升级为编译时数学证明**。

---

## 📐 Lean 4 是什么

Lean 4 是一个**交互式定理证明器**，由微软研究院开发（2013年至今），本质上是一个**能把数学命题当代码编译的编程语言**。

### 通俗理解

普通编程语言（Python/Rust）检查的是**代码能不能跑**（语法、类型安全），Lean 4 检查的是**逻辑对不对**（数学证明）。

### 在AI交易领域的应用

```
场景："交易量不能超过账户余额的50%"

普通写法（我们现在的做法）：
  if trade_amount > balance * 0.5:
      reject("超出额度")

Lean 4写法（论文方案）：
  把"交易量 ≤ 余额×50%" 写成数学定理
  Lean内核验证这个定理对当前输入是否成立
  成立 → 放行    不成立 → 拦截 + 形式化错误日志
```

### 关键优势

| 特性 | 普通条件检查 | Lean 4 验证 |
|:-----|:-----------|:------------|
| 验证方式 | 运行时if判断 | 编译时数学证明 |
| 结果确定性 | 概率性（可能被绕过） | **二进制**（True/False，数学保证） |
| 审计能力 | 日志记录 | 形式化证明链（可独立验证） |
| 延迟 | if判断 < 1μs | **5μs**（论文实测，相近量级） |
| 复杂约束 | 难以组合多层规则 | 定理可组合推理 |

### 为什么对我们有价值

我们当前的 `price修正`（AI触发价偏离`[固定@30]`超过2%时自动拉回）是运行时if判断。如果换成 Lean 4 定理验证：
- 价格约束变成**数学定理**
- 交易前不用等LLM判断，5μs内就能确定是否合规
- 每一笔被拦截的交易都有**形式化证明记录**，用户可以独立验证

### 局限

- 自然语言转 Lean 4 定理需要专门的**神经符号模型**（论文用 Aristotle 模型做 auto-formalization）
- 不是所有约束都能形式化（"感觉市场要跌"这类模糊判断不行）
- 需要额外的编译/部署流程，不能直接嵌入Python运行时

