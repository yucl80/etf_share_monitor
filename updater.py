# -*- coding: utf-8 -*-
"""数据更新：本地优先，缺失数据自动到官网补全。

数据优先级：
  1. 交易所官方日频份额（沪：上交所按日接口；深：深交所区间接口）
     —— 权威口径，可直接补出 1日/1周/1月/3月/6月 全部窗口；
  2. 天天基金 F10 季度末总份额 —— 补足更早的历史；
  3. 腾讯行情（总市值÷最新价）—— 仅用于交易所报表未覆盖的代码兜底。
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import datasource as ds
import storage
from config import F10_WORKERS, META_REFRESH_DAYS, QUOTE_BATCH, SSE_PROBE_BACK_DAYS, SZSE_HISTORY_DAYS

# 本轮抓取的失败项（供 run_update 汇总：核心数据全部拿到才算成功，才允许「今日不再重抓」）
_ERRORS = []


def _err(msg):
    _ERRORS.append(msg)
    print(f"  ! {msg}")


def _last_quarter_end(today):
    """最近一个已结束的季度末日期（官方规模变动按季度披露）。"""
    q_month = ((today.month - 1) // 3) * 3 + 1
    first_of_quarter = datetime(today.year, q_month, 1)
    return (first_of_quarter - timedelta(days=1)).strftime("%Y-%m-%d")


def update_exchange_official(etfs):
    """步骤 1+2：交易所官方份额（沪市日频 + 深市日频区间 + 深市拟合指数映射）。

    返回 (official, stats)：
      * official = {code: (date, shares)}，各 ETF 官方口径的最新快照；
      * stats    = {"szse_rows": 深市入库条数, "sse_rows": 沪市入库条数}
                   —— 两者均非 0 才视为本次核心数据抓取成功。
    """
    today = datetime.now()
    official = {}

    # ---- 深市：一次区间请求拿到近半年日频份额 ----
    start, end = ds.szse_history_window(SZSE_HISTORY_DAYS)
    try:
        rows = ds.fetch_szse_scale(start, end)
    except Exception as e:  # noqa: BLE001
        _err(f"深交所规模接口失败: {e}")
        rows = []
    if rows:
        storage.upsert_shares_many([(code, date, shares, "szse") for date, code, _, shares in rows])
        for date, code, _, shares in rows:
            if code not in official or date > official[code][0]:
                official[code] = (date, shares)
    print(f"[1/4] 深交所官方日频份额入库: {len(rows)} 条，覆盖 {len(official)} 只（区间 {start} ~ {end}）")

    # ---- 深市：ETF 列表（拟合指数 -> 指数代码 + 官方当期份额）----
    sz_meta = {}
    try:
        sz_meta = ds.fetch_szse_etf_list()
    except Exception as e:  # noqa: BLE001
        _err(f"深交所ETF列表失败: {e}")
    for code, info in sz_meta.items():
        if info.get("index_code"):
            storage.upsert_meta(code, info["name"], info.get("index_name"),
                                info["index_code"], index_source="exchange")
    print(f"      深市ETF列表: {len(sz_meta)} 只，其中带拟合指数 "
          f"{sum(1 for v in sz_meta.values() if v.get('index_code'))} 只")

    # ---- 沪市：近两周逐日取数（保证 1日/1周 有连续交易日基准）+ 月/季锚点 ----
    today_str = today.strftime("%Y-%m-%d")
    recent = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(0, 15)]
    distant = [(today - timedelta(days=d)).strftime("%Y-%m-%d") for d in (30, 91, 182)]
    sse_rows, resolved, seen_dates = [], {}, set()

    def _collect(target, date, data):
        resolved[target] = date
        if date not in seen_dates:
            seen_dates.add(date)
            sse_rows.extend((code, date, shares, "sse") for code, shares in data.items())
        for code, shares in data.items():
            if code not in official or date > official[code][0]:
                official[code] = (date, shares)

    for target in recent:                      # 非交易日返回空，自动跳过
        try:
            data = ds.fetch_sse_shares(target)
        except Exception as e:  # noqa: BLE001
            _err(f"上交所份额接口失败({target}): {e}")
            continue
        if data:
            _collect(target, target, data)
    for target in distant:                     # 向前回溯找最近交易日
        try:
            date, data = ds.fetch_sse_nearest(target, max_back=SSE_PROBE_BACK_DAYS)
        except Exception as e:  # noqa: BLE001
            _err(f"上交所份额接口失败({target}): {e}")
            continue
        if date:
            _collect(target, date, data)
    if sse_rows:
        storage.upsert_shares_many(sse_rows)
    print(f"[2/4] 上交所官方日频份额入库: {len(sse_rows)} 条，覆盖交易日 {sorted(seen_dates)[:3]}"
          f"...{sorted(seen_dates)[-3:]}（月/季锚点 "
          + ", ".join(f"{k[5:]}→{v[5:]}" for k, v in resolved.items() if k in distant) + "）")

    return official, {"szse_rows": len(rows), "sse_rows": len(sse_rows)}


def update_quote_fallback(etfs, official):
    """步骤 3：对交易所报表未覆盖的代码，用行情推算份额兜底。"""
    codes = [c for c, _ in etfs if c not in official]
    if not codes:
        print("[3/4] 全部 ETF 均有交易所官方数据，无需行情兜底")
        return
    quotes = ds.fetch_quotes(codes, batch_size=QUOTE_BATCH)
    if quotes:
        storage.upsert_shares_many([(c, q["date"], q["shares"], "quote") for c, q in quotes.items()])
    print(f"[3/4] 行情兜底: {len(quotes)} / {len(codes)} 只（交易所报表未覆盖）")


def _fetch_f10(code, known_index_code=None):
    """单只基金：跟踪指数（名称 + 代码 + 来源）+ 季度末份额历史。

    指数代码解析顺序（已由交易所/手工确认的 code 不重复解析）：
      1. 交易所官方映射（深市「拟合指数」）——即 known_index_code，来源 exchange
      2. F10 跟踪标的名称 -> 官方指数字典 / 东财搜索，来源 dict / search
      3. 东方财富基金详情接口 INDEXCODE —— 权威兜底，来源 fund_api，
         覆盖标普/MSCI/富时/恒生/中债等境外与债券指数
    """
    index_name, index_code, source, gmbd = None, known_index_code, None, []
    try:
        index_name = ds.fetch_index_name(code)
    except Exception as e:  # noqa: BLE001
        print(f"  ! 概况抓取失败 {code}: {e}")
    if not index_code and index_name:
        try:
            index_code, source = ds.resolve_index_code_with_source(index_name)
        except Exception as e:  # noqa: BLE001
            print(f"  ! 指数解析失败 {code}: {e}")
    if not index_code:
        try:
            api_code, api_name = ds.fetch_fund_index(code)
            index_code, source = api_code, ("fund_api" if api_code else None)
            if not index_name:
                index_name = api_name
        except Exception as e:  # noqa: BLE001
            print(f"  ! 基金详情接口失败 {code}: {e}")
    try:
        gmbd = ds.fetch_gmbd_shares(code)
    except Exception as e:  # noqa: BLE001
        print(f"  ! 规模变动抓取失败 {code}: {e}")
    return code, index_name, index_code, source, gmbd


def update_f10(etfs):
    """步骤 4：F10 补全（跟踪指数映射 + 更早的季度末历史）。

    以下三种情况才抓取（同一基金在 META_REFRESH_DAYS 天内不重复抓取）：
      * 从未抓过 / 缓存过期；
      * 缺少最近一个季度末的份额；
      * **跟踪指数代码仍为空**（修复历史遗留的未识别映射）。
    """
    meta = storage.get_meta()
    today = datetime.now()
    last_qe = _last_quarter_end(today)
    need = []
    for code, _name in etfs:
        m = meta.get(code)
        fresh = bool(m and m.get("f10_updated") and (
            today - datetime.strptime(m["f10_updated"], "%Y-%m-%d")).days < META_REFRESH_DAYS)
        if not fresh or last_qe not in storage.get_share_dates(code) or not (m or {}).get("index_code"):
            need.append(code)
    print(f"[4/5] 需补全 F10 的基金: {len(need)} 只")
    if not need:
        return
    names = dict(etfs)
    done, resolved_new, by_src = 0, 0, {}
    with ThreadPoolExecutor(max_workers=F10_WORKERS) as pool:
        futures = {pool.submit(_fetch_f10, code, (meta.get(code) or {}).get("index_code")): code
                   for code in need}
        for fut in as_completed(futures):
            code, index_name, index_code, src, gmbd = fut.result()
            old = (meta.get(code) or {}).get("index_code")
            if index_code and not old:
                resolved_new += 1
                by_src[src] = by_src.get(src, 0) + 1
            storage.upsert_meta(code, names.get(code), index_name, index_code,
                                f10_updated=storage.today_str(),
                                index_source=None if old else src)
            have = storage.get_share_dates(code)
            new_rows = [(code, d, s, "gmbd") for d, s in gmbd if d not in have]
            if new_rows:
                storage.upsert_shares_many(new_rows)
            done += 1
            if done % 100 == 0 or done == len(need):
                print(f"      F10 进度: {done}/{len(need)}")
    print(f"      本轮新解析出跟踪指数代码 {resolved_new} 只 " +
          ("| " + "、".join(f"{k}:{v}" for k, v in by_src.items()) if by_src else ""))


def correct_index_mappings():
    """步骤 5：纠正历史遗留的**错误**指数映射。

    只对非「交易所官方映射」的记录做纠正，且仅接受官方指数的**全称**精确匹配
    （full_name_only），避免把同名不同机构的指数改错
    （如「新能电池」同为国证 980032 与中证 931555，两者都是真实存在的指数）。
    """
    import index_dict
    meta = storage.get_meta()
    changed, checked = [], 0
    for code, m in meta.items():
        idx_name, old, src = m.get("index_name"), m.get("index_code"), m.get("index_source")
        if not idx_name or not old or src == "exchange" or src == "alias":
            continue
        checked += 1
        try:
            new, hit = index_dict.lookup_official(idx_name, full_name_only=True)
        except Exception:  # noqa: BLE001
            continue
        if new and new != old:
            changed.append((code, m.get("name"), idx_name, old, new))
            storage.upsert_meta(code, m.get("name"), idx_name, new, index_source="dict")
    print(f"[5/5] 映射纠错: 检查 {checked} 只，修正 {len(changed)} 只")
    for code, name, idx_name, old, new in changed[:15]:
        print(f"      {code} {name}: {idx_name} {old} -> {new}")
    if len(changed) > 15:
        print(f"      ...（其余 {len(changed) - 15} 只同类修正）")
    return len(changed)


def _resolve_only(code, known_index_code=None):
    """只做跟踪指数映射解析（不抓份额历史），用于 `main.py index`。"""
    index_name, index_code, source = None, known_index_code, None
    try:
        index_name = ds.fetch_index_name(code)
    except Exception as e:  # noqa: BLE001
        print(f"  ! 概况抓取失败 {code}: {e}")
    if not index_code and index_name:
        try:
            index_code, source = ds.resolve_index_code_with_source(index_name)
        except Exception as e:  # noqa: BLE001
            print(f"  ! 指数解析失败 {code}: {e}")
    if not index_code:
        try:
            api_code, api_name = ds.fetch_fund_index(code)
            index_code, source = api_code, ("fund_api" if api_code else None)
            index_name = index_name or api_name
        except Exception as e:  # noqa: BLE001
            print(f"  ! 基金详情接口失败 {code}: {e}")
    return code, index_name, index_code, source


def fix_index_mappings():
    """只修复跟踪指数映射：先补全缺失，再纠正错误（不动份额数据）。"""
    etfs = ds.fetch_etf_list()
    print(f"获取到全市场场内 ETF {len(etfs)} 只")
    _preload_index_dict()
    meta = storage.get_meta()
    need = [c for c, _ in etfs if not (meta.get(c) or {}).get("index_code")]
    print(f"跟踪指数代码缺失: {len(need)} 只 -> 开始解析")
    if need:
        done, ok, by_src = 0, 0, {}
        with ThreadPoolExecutor(max_workers=F10_WORKERS) as pool:
            futs = {pool.submit(_resolve_only, c): c for c in need}
            for fut in as_completed(futs):
                code, index_name, index_code, source = fut.result()
                if index_code:
                    ok += 1
                    by_src[source] = by_src.get(source, 0) + 1
                storage.upsert_meta(code, dict(etfs).get(code), index_name, index_code,
                                    index_source=source)
                done += 1
                if done % 100 == 0 or done == len(need):
                    print(f"      进度: {done}/{len(need)}")
        print(f"      解析成功 {ok}/{len(need)} 只 | 来源 "
              + "、".join(f"{k}:{v}" for k, v in by_src.items()))
    correct_index_mappings()


def run_update():
    """抓取并补全数据，并把本次结果写入运行状态表。

    返回摘要（供入口判断能否「今日不再重复抓取」）：
      {"ok": bool, "szse_rows": int, "sse_rows": int,
       "latest_snapshot": str|None, "errors": [str, ...]}
      * ok=True 表示**核心数据**（沪深交易所日频份额）本轮均成功获取；
      * 只有 ok=True 时，当日再次运行才会跳过网络抓取。
    """
    _ERRORS.clear()
    etfs = ds.fetch_etf_list()
    print(f"获取到全市场场内 ETF {len(etfs)} 只")
    _preload_index_dict()
    official, stats = update_exchange_official(etfs)
    update_quote_fallback(etfs, official)
    update_f10(etfs)
    correct_index_mappings()

    latest = storage.latest_share_date()
    ok = stats["szse_rows"] > 0 and stats["sse_rows"] > 0
    now = datetime.now()
    storage.set_states({
        "last_fetch_date": now.strftime("%Y-%m-%d"),
        "last_fetch_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "last_fetch_ok": "1" if ok else "0",
        "latest_snapshot": latest or "",
        "last_fetch_errors": str(len(_ERRORS)),
    })
    print(f"[数据抓取完成] 深市 {stats['szse_rows']} 条 / 沪市 {stats['sse_rows']} 条 | "
          f"最新快照 {latest or '无'} | 核心数据{'成功' if ok else '失败'} | "
          f"失败 {len(_ERRORS)} 项")
    if not ok:
        print("      ⚠ 核心数据未完整获取，本次不记录为「今日已抓取」，下次运行会重试")
    return {"ok": ok, "latest_snapshot": latest, "errors": list(_ERRORS), **stats}


def _preload_index_dict():
    """预载官方指数字典（中证指数官网 + 国证指数官网），供跟踪指数解析使用。"""
    try:
        import index_dict
        st = index_dict.stats()
        print(f"官方指数字典: 全称 {st['全称']} 条 / 简称 {st['简称']} 条（更新于 {st['updated']}）")
    except Exception as e:  # noqa: BLE001
        print(f"  ! 官方指数字典加载失败（将退回东财检索 + 基金详情接口）: {e}")


if __name__ == "__main__":
    run_update()
