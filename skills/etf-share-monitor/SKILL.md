---
name: etf-share-monitor
description: 获取A股/场内ETF基金份额数据并生成指数维度汇总报告。覆盖ETF全量名单、交易所官方日频份额（沪市上交所按日接口、深市深交所区间接口）、深市拟合指数映射、官网F10季度末历史补全，输出按指数代码汇总的可排序HTML表格（份额总数 + 最近1日/1周/1月/3月/6月变化）。触发词：ETF份额、ETF份额变化、基金份额监控、ETF资金流、份额变动报告、ETF指数维度汇总。
agent_created: true
---

# A股 ETF 份额变化监控

## 用途
拉取全市场场内 ETF 份额，本地 SQLite 增量存储（本地缺失自动到官网补全），
按**跟踪指数代码**维度汇总，输出可排序 HTML 报告（1日/1周/1月/3月/6月变化）。

## 现成工程
`<workspace>/etf_share_monitor/`：`config.py` `storage.py` `datasource.py` `index_dict.py` `updater.py` `report.py` `main.py` `requirements.txt` `data/index_alias.json`
**依赖为 0**：纯标准库实现（`urllib` 请求 / `zipfile`+`ElementTree` 解析 xlsx / `sqlite3` 存储 / `concurrent.futures` 并发），`requirements.txt` 只有注释。新增功能时不要引入第三方包，否则会破坏这一约束。Python >= 3.8。
本 skill 的副本随工程一起维护在仓库 `skills/etf-share-monitor/SKILL.md`：
https://github.com/yucl80/etf_share_monitor （改动后记得同步两边）
用法：`python main.py`（更新+报告）/ `update`（仅更新份额+映射）/ `index`（仅修复跟踪指数映射：补缺失+纠错）/ `report`
首跑约 2~10 分钟，增量运行 1~3 分钟（深市区间请求 + 沪市近两周逐日）。

## 数据源（关键：份额历史必须用交易所官方接口）

| 用途 | 接口 | 关键点 |
|---|---|---|
| ETF 全量名单 | `fund.eastmoney.com/js/fundcode_search.js` | 过滤：6位代码且 `5xxxxx`(沪) / `15,16,18xxxx`(深)；名称含 `ETF`；排除 `联接`/`FOF`/`货币` → 约 1700 只 |
| **沪市官方日频份额** | `query.sse.com.cn/commonQuery.do`，`sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L`，参数 `STAT_DATE=YYYY-MM-DD`，`pageHelp.pageSize=10000`，Referer `https://www.sse.com.cn/` | 单次返回当日**全部沪市 ETF**（约 900 只）；字段 `SEC_CODE`/`SEC_NAME`/`TOT_VOL`(**万份 → ×1e4 得份**)；非交易日返回空；历史可回溯多年 |
| **深市官方日频份额** | `www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=scsj_fund_jjgm&TABKEY=tab1&txtStart=YYYY-MM-DD&txtEnd=YYYY-MM-DD&jjlb=ETF&random=<r>`，Referer `https://www.szse.cn/market/fund/volume/etf/index.html` | **仅支持 xlsx**（JSON 报 500）；单次区间 ≤ **6 个月**；列 = 日期/基金代码/基金简称/**基金规模(份)**；单元格是 `t="inlineStr"`；约 2.7MB / 12s per 半年 |
| **深市拟合指数映射** | `www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1945&TABKEY=tab1&random=<r>`，Referer `https://www.szse.cn/market/fund/etf/index.html` | 全量 743 只，列含**拟合指数**（如 `399372 大盘成长` = 指数代码+名称）+ 当前规模(份) → 一次性解决深市指数映射 |
| 兜底份额快照 | `qt.gtimg.cn/q=sh510300,sz159915`（GBK） | `~` 分割：f[3]=最新价、f[30]=行情时间戳、f[45]=总市值(亿元)；份额=f45×1e8÷f3；**仅用于交易所报表未覆盖的代码** |
| 历史份额(季度末) | `fundf10.eastmoney.com/FundArchivesDatas.aspx?type=gmbd&code=510300` | 必须带 Referer `https://fundf10.eastmoney.com/`；含「期末总份额（亿份）」；用于补足更早历史 |
| 跟踪指数名称 | `fundf10.eastmoney.com/jbgk_510300.html` | 正则 `跟踪标的</th><td>([^<]+)</td>` |
| **指数全量字典（中证）** | `POST www.csindex.com.cn/csindex-home/exportExcel/indexAll/CH`，body `{"sorter":{...},"pager":{...},"indexFilter":{...,"indexSeries":null,...}}`，`Content-Type: application/json;charset=UTF-8` | **必须 POST**（GET 返回 404/非 xlsx）；返回 **xlsx**（不是 JSON！）；表头含 `指数代码/指数简称/指数全称/指数系列…`；`indexSeries=null` 覆盖中证+上证系列约 3000 条，耗时 ~17s |
| **指数全量字典（国证）** | `GET www.cnindex.com.cn/index/indexList?channelCode=-1&rows=5000&pageNum=1`，Referer `https://www.cnindex.com.cn/` | JSON：`data.rows[].indexcode / indexname(简称) / indexfullcname(全称)`，约 1480 条。**搜索参数（indexName/searchText）被忽略，只能拉全量本地匹配** |
| **跟踪指数代码（权威兜底）** | `fundmobapi.eastmoney.com/FundMNewApi/FundMNBasicInformation?FCODE=513030&deviceid=1&plat=Iphone&product=EFund&version=1`，Referer `https://fund.eastmoney.com/` | 直接返回 `Datas.INDEXCODE` / `Datas.INDEXNAME` —— **最可靠**，能给出标普/MSCI/富时/恒生/中债/DAX 等检索库查不到的指数代码。⚠️**限流极严**：连续几十次后返回 `ErrCode=61136403 网络繁忙`（IP 级、持续数十分钟），必须全局限速 ≥0.4s + 指数退避 |
| 指数名称→代码（兜底） | `searchadapter.eastmoney.com/api/suggest/get?input=<kw>&type=14&count=15` | 类别 `Classify ∈ {Index, "24", NDI, HK, UniversalIndex, SGE}`（**中证系是字符串 "24"**）+ difflib 相似度 ≥0.6；**仅作最后兜底**，覆盖不全 |

## 采集策略（决定 1日/1周/1月 能否填满）
- **沪市**：先取「近 15 个自然日逐日」（非交易日返回空，自动跳过），保证 **1日/1周有连续交易日基准**；
  再对 30/91/182 天前的目标日**向前回溯**最多 12 天找最近交易日。
- **深市**：一次区间请求拿近 180 天全量日频 → 所有窗口都有精确基准。
- **历史基准选取**（report）：在 `[今日-N±容限]` 内取距目标日最近的记录（允许基期略晚），
  且基期须早于当前快照 `N/2` 天；容限 1d:4 / 1w:6 / 1m:20 / 3m:45 / 6m:60 天。

## 跟踪指数映射（最容易踩坑的一环）

「跟踪标的名称 → 指数代码」**不要指望东财检索接口**：它对中证/国证细分主题指数覆盖极差
（「中证电池主题指数」「上证科创板综合指数」「国证航天航空行业指数」「中证港股通高股息投资港元指数」
都搜不到），曾导致 396 只 ETF（占 23%）无法归入指数维度。正确做法是分级解析：

```
① 价 source='exchange'  深市：深交所 ETF 列表「拟合指数」列（730/743 只覆盖）
②   source='alias'       本地 data/index_alias.json 手工别名（最高优先级，人工 override）
③   source='dict'        官方指数全量字典（中证官网 ~3000 + 国证官网 ~1480 条）名称匹配
④   source='fund_api'    东财基金详情接口 Datas.INDEXCODE（权威，限流严，仅对仍缺失的调用）
⑤   source='search'      东财 searchadapter（最后兜底）
```
`etf_meta.index_source` 记录来源。实测：1696/1696 全部映射成功（dict 431 / exchange 679 / alias 34 …其余为历史行）。

### 名称匹配的归一化规则（index_dict.normalize）
去括号内容 → 去空白 → **去币种标记（港元/港币/人民币/美元/离岸人民币，任意位置）** → 去尾部修饰词（全收益指数/净收益指数/全收益/收益率/指数/价格/净值）。
解决三类官方全称与「跟踪标的」写法不一致：
- 币种后缀：`中证港股通高股息投资港元指数` → `中证港股通高股息投资` → 930914
- 空格差异：`沪深300ESG基准指数` vs 官方 `沪深300 ESG基准指数` → 931463
- 括号修饰：`上证5年期国债指数` vs 官方 `上证5年期国债指数(全价)` → H00140

### 补全 vs 纠错，规则必须分开（关键教训）
- **补全缺失**：精确全称 → 精确简称 → 归一化全称 → 归一化简称，都可接受。
- **纠正已有映射**：**只接受「官方全称」精确匹配**（`lookup_official(name, full_name_only=True)`）。
  因为**同名不同机构是常态**：`新能电池` 同为国证 980032 与中证 931555，`通用航空` 同为 980076/931855，
  `绿色电力` 同为 399438/931897，`消费电子` 同为 980030/931494 —— 用简称去「纠正」会把深交所拟合指数
  给出的正确代码改错。只按全称纠错时：`中证全指自由现金流指数` 980092→932365 ✓、
  `中证新能源指数` 000941→399808 ✓、`上证综合指数` 000008→000001 ✓，而 6 只 `新能电池` 的正确代码 980032 保持不变 ✓。
- 纠错前先跑 `update_exchange_official`（把深市 source 标为 exchange），纠错时 `source=='exchange'/'alias'` 一律跳过。

## 已知坑
- **不要用 `push2.eastmoney.com` / `push2delay.eastmoney.com`**：Windows curl(schannel) TLS 重协商失败；Python 连续请求也被限流返回空。指数列表/行情别指望它（`s:5` 板块只给 103 个指数，远非全量）。
- **中证官网导出接口必须 POST**：`urllib.request.Request(url, headers=...)` 不带 `data=` 会自动变 GET，返回 404 或非 xlsx → 必须显式 `data=json.dumps(payload).encode(), method="POST"`。
- **东财基金详情接口限流极严**：批量并发几十次后整段被拒（`ErrCode=61136403`），且是 IP 级、恢复要等数十分钟。必须全局限速（≥0.4s/次）+ 指数退避，且**只对名称解析仍失败的少数标的调用**（本次仅 34 只，别名补齐后为 0）。
- **深交所有两个易混接口**：`CATALOGID=1945`（ETF列表，**当前**份额+拟合指数，`txtQueryDate` 被忽略）与 `CATALOGID=scsj_fund_jjgm`（**日频历史**规模，用 `txtStart/txtEnd`）。历史份额必须用后者。
- 深交所 xlsx 单元格是 `t="inlineStr"`（`<is><t>`），不是 sharedStrings；不用 pandas/openpyxl 也能用 zipfile + ElementTree 解析。
- **入库元组顺序固定为 `(code, date, shares, source)`**——曾把 SSE 写成 `(date, code, ...)`，导致日期写进代码列、报告解析日期崩溃。写库后抽检 `date not like '____-__-__'`。
- 交易所份额报表含货币型（159001/159003/159005）等**不在 ETF 名单内**的品种，报告侧 `get_all_shares()` 必须 JOIN `etf_meta` 过滤，否则会以「未识别跟踪指数」名义出现在报告里。
- F10 抓取需限频：同一基金 `META_REFRESH_DAYS`(30) 天内只抓一次；但**「index_code 为空」必须强制重抓**（否则历史遗留的未识别映射永远不会被修复）。
- **切勿**用已解析指数做模糊兜底匹配（会把「中证稀有金属主题指数」错配成「中证有色金属指数」）。
- 报告生成时 **CSS/JS 不要写在 f-string 里**（大括号极易漏转义）→ 抽成普通字符串常量，再用 f-string 插入。

## 输出
`output/etf_index_share_report.html`：列 = 指数代码、指数名称、ETF数量、份额总数(亿份)、最近1日/1周/1月/3月/6月变化（变化量+百分比+基期日期，title 显示偏差天数）；
点击表头排序（默认按最近1月降序，空值恒排末尾）；**份额增加红色、减少绿色**（A股惯例）。
**点击任意指数行 → 弹出成分 ETF 明细弹窗**（代码/名称/份额总数/各窗口增减，可排序、底部合计行，Esc/遮罩/×关闭）。
实现要点：明细数据以内嵌 `<script type="application/json">` 紧凑载荷传入前端（`report.build_detail_payload`；ETF 窗口基期与组内默认一致时存 `null` 不重复存储；`</` 需转义为 `<\/`）；CSS/JS 用普通字符串常量（`_CSS`/`_JS`）再经 f-string 插入，避免大括号转义地狱；数值排序读单元格 `data-v` 而非 innerText（千分位逗号曾致 parseFloat 解析错误）。
浏览器端验证方法：`playwright-core` + 已缓存的 `ms-playwright/chromium_headless_shell-*`（`executablePath` 显式指定，版本不必严格匹配），断言排序/弹窗/勾稽合计/无 console error。
实测：**492 个指数维度、1667 只 ETF、未识别跟踪指数 0 只**，2460 个汇总变化单元格中仅 3 个为空（成分 ETF 级明细空窗口约 3%）。
