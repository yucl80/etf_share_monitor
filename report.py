# -*- coding: utf-8 -*-
"""报告生成：以指数代码为维度汇总 ETF 份额变化，输出可排序 HTML 表格。"""
import html
from datetime import datetime

import storage
from config import REPORT_PATH, WINDOWS

_YI = 1e8  # 份 -> 亿份


def _parse(d):
    return datetime.strptime(d, "%Y-%m-%d")


def compute_etf_changes():
    """每只 ETF：当前份额 + 各窗口变化。

    基准记录选取规则：在 [今日-N-容限, 今日-N+容限] 内取距目标日最近的份额记录，
    且基期须早于当前快照 window/2 天以上（避免基期与当前过于接近）。
    数据源优先级：本地每日快照（day级，随运行积累）> 官网季度末总份额（官方最细粒度）。
    因此 1日/1周精度随每日运行逐步精化，3月/6月首次运行即可由季度末数据给出。
    """
    all_shares = storage.get_all_shares()
    meta = storage.get_meta()
    today = datetime.now()
    result = {}
    for code, records in all_shares.items():
        if not records:
            continue
        records = sorted(records)
        cur_date, cur = records[-1]
        windows = {}
        for key, _label, days, tol in WINDOWS:
            target = today.timestamp() - days * 86400
            newest_allowed = _parse(cur_date).timestamp() - days * 86400 * 0.5
            best, best_gap = None, None
            for d, s in records:
                ts = _parse(d).timestamp()
                if ts > newest_allowed:      # 基期须明显早于当前快照
                    continue
                gap = abs(target - ts) / 86400
                if gap > tol:
                    continue
                if best_gap is None or gap < best_gap:
                    best, best_gap = (d, s), gap
            if best is None:
                windows[key] = None
                continue
            windows[key] = {
                "delta": cur - best[1],
                "pct": (cur - best[1]) / best[1] if best[1] else None,
                "base_date": best[0],
                "cur_date": cur_date,
                "gap": round(best_gap, 1),
            }
        result[code] = {
            "name": (meta.get(code) or {}).get("name", code),
            "index_name": (meta.get(code) or {}).get("index_name"),
            "index_code": (meta.get(code) or {}).get("index_code"),
            "shares": cur,
            "cur_date": cur_date,
            "windows": windows,
        }
    return result


def aggregate_by_index(etf_changes):
    """按指数代码汇总：份额总数 + 各窗口变化求和。"""
    groups = {}
    for code, info in etf_changes.items():
        if not info["index_code"]:
            continue
        g = groups.setdefault(info["index_code"], {
            "index_name": info["index_name"] or info["index_code"],
            "etf_count": 0,
            "shares": 0.0,
            "windows": {k: {"delta": 0.0, "pct_den": 0.0, "hit": 0, "bases": {}, "cur_date": ""}
                        for k, _, _, _ in WINDOWS},
        })
        g["etf_count"] += 1
        g["shares"] += info["shares"]
        for k, _, _, _ in WINDOWS:
            w = info["windows"].get(k)
            if w:
                gw = g["windows"][k]
                gw["delta"] += w["delta"]
                gw["pct_den"] += (info["shares"] - w["delta"])  # 基期份额
                gw["hit"] += 1
                gw["bases"][w["base_date"]] = gw["bases"].get(w["base_date"], 0) + 1
                gw["cur_date"] = max(gw["cur_date"], w["cur_date"])
    rows = []
    for idx_code, g in groups.items():
        row = {
            "index_code": idx_code,
            "index_name": g["index_name"],
            "etf_count": g["etf_count"],
            "shares": g["shares"],
        }
        for k, _label, days, _tol in WINDOWS:
            w = g["windows"][k]
            if w["hit"] and w["pct_den"]:
                # 基期取构成 ETF 中出现最多的日期；不同 ETF 基期不一致时给出可读提示
                base = max(w["bases"].items(), key=lambda kv: kv[1])[0]
                row[k] = {"delta": w["delta"], "pct": w["delta"] / w["pct_den"], "hit": w["hit"],
                          "base_date": base, "base_variants": len(w["bases"]),
                          "cur_date": w["cur_date"],
                          "gap": abs((datetime.now() - _parse(base)).days - days)}
            else:
                row[k] = None
        rows.append(row)
    return rows


def _fmt_num(v, digits=2):
    return f"{v:,.{digits}f}" if v is not None else "—"


def _cell(w):
    """窗口变化单元格：份额变化(亿份) + 百分比，红涨绿跌。"""
    if not w:
        return '<td class="num muted" title="本地暂无可用基期数据（日频快照需每日运行积累，或官网尚未披露新季度）">—</td>'
    delta_yi = w["delta"] / _YI
    pct = w.get("pct")
    cls = "up" if delta_yi > 0 else ("down" if delta_yi < 0 else "")
    pct_txt = f'<div class="pct">{"+" if pct > 0 else ""}{pct * 100:.2f}%</div>' if pct is not None else ""
    sign = "+" if delta_yi > 0 else ""
    base = w.get("base_date") or "—"
    extra = f"，{w['base_variants']}种基期" if w.get("base_variants", 1) > 1 else ""
    tip = f'基期份额日期 {base}（距目标日 {w.get("gap", "?")} 天），快照日 {w.get("cur_date", "")}{extra}'
    return (f'<td class="num {cls}" title="{tip}">{sign}{delta_yi:,.2f}<span class="unit">亿份</span>'
            f'{pct_txt}<div class="basedate">基期 {base[5:] if len(base) == 10 else base}</div></td>')


def render_html(rows, unmatched, total_etf, cur_date):
    keys = [k for k, _, _, _ in WINDOWS]
    labels = {k: lb for k, lb, _, _ in WINDOWS}

    # 默认按最近1月变化降序
    def sort_key(r):
        w = r.get("1m")
        return w["delta"] if w else float("-inf")
    rows = sorted(rows, key=sort_key, reverse=True)

    body = []
    for r in rows:
        cells = [
            f'<td class="code">{r["index_code"]}</td>',
            f'<td>{html.escape(r["index_name"])}</td>',
            f'<td class="num">{r["etf_count"]}</td>',
            f'<td class="num strong">{r["shares"] / _YI:,.2f}</td>',
        ]
        cells += [_cell(r.get(k)) for k in keys]
        body.append("<tr>" + "".join(cells) + "</tr>")

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    ths = "".join(
        f'<th class="sortable num" data-k="{k}" data-t="num" title="点击排序">{labels[k]}</th>'
        for k in keys
    )
    legend = "、".join(f"{labels[k]}允许基期偏差±{tol}天" for k, lb, d, tol in WINDOWS if k in ("1m", "3m"))
    un_rows = "".join(
        f'<li>{c} {html.escape(v["name"])} —— 跟踪标的：{html.escape(v["index_name"] or "未披露")}</li>'
        for c, v in sorted(unmatched)
    )
    un_block = (f'<details><summary>未纳入汇总的 ETF（{len(unmatched)} 只，跟踪标的未在指数库中检索到）</summary>'
                f'<ul class="unlist">{un_rows}</ul></details>') if unmatched else ""

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>A股ETF份额变化报告（按跟踪指数汇总）</title>
<style>
  :root {{ --up:#d93026; --down:#0a8f4a; --bg:#f5f6f8; --card:#ffffff; --line:#e3e6ea;
          --txt:#1f2733; --muted:#8a94a3; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:24px; background:var(--bg); color:var(--txt);
         font:14px/1.5 "Microsoft YaHei","PingFang SC",-apple-system,sans-serif; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  .sub {{ color:var(--muted); font-size:12px; margin-bottom:16px; }}
  .cards {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:16px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:8px;
          padding:10px 16px; min-width:150px; }}
  .card .v {{ font-size:20px; font-weight:700; }}
  .card .t {{ color:var(--muted); font-size:12px; }}
  .table-wrap {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
                overflow:auto; max-height:78vh; }}
  table {{ border-collapse:collapse; width:100%; min-width:1080px; }}
  th, td {{ padding:9px 12px; border-bottom:1px solid var(--line); white-space:nowrap; }}
  thead th {{ position:sticky; top:0; background:#fafbfc; z-index:2; font-size:13px;
             border-bottom:2px solid #d5dae1; text-align:right; }}
  thead th:nth-child(1), thead th:nth-child(2) {{ text-align:left; }}
  th.sortable {{ cursor:pointer; user-select:none; }}
  th.sortable:hover {{ background:#eef1f5; }}
  th .arrow {{ font-size:10px; color:var(--muted); }}
  tbody tr:hover {{ background:#f6f9fd; }}
  td {{ text-align:right; }}
  td:nth-child(1), td:nth-child(2) {{ text-align:left; }}
  td.code {{ font-family:Consolas,Menlo,monospace; font-weight:600; }}
  .num .unit {{ font-size:11px; color:var(--muted); margin-left:3px; }}
  .up {{ color:var(--up); }} .down {{ color:var(--down); }}
  .muted {{ color:var(--muted); }}
  .strong {{ font-weight:700; }}
  .pct {{ font-size:11px; }}
  .basedate {{ font-size:10px; color:var(--muted); }}
  .note {{ color:var(--muted); font-size:12px; margin-top:12px; line-height:1.8; }}
  details {{ margin-top:10px; background:var(--card); border:1px solid var(--line);
             border-radius:8px; padding:10px 14px; font-size:12px; color:var(--muted); }}
  details summary {{ cursor:pointer; color:var(--txt); }}
  .unlist {{ columns:3; margin:8px 0 0; padding-left:18px; }}
  .unlist li {{ break-inside:avoid; line-height:1.9; }}
</style>
</head>
<body>
<h1>A股 ETF 份额变化报告 · 按跟踪指数汇总</h1>
<div class="sub">生成时间 {now} · 份额快照交易日 {cur_date} · 覆盖 ETF {total_etf} 只 · 纳入指数维度 {len(rows)} 个 · 未识别跟踪指数 {len(unmatched)} 只（见文末明细）</div>

<div class="cards">
  <div class="card"><div class="v">{len(rows)}</div><div class="t">指数维度数</div></div>
  <div class="card"><div class="v">{total_etf}</div><div class="t">覆盖 ETF 数</div></div>
  <div class="card"><div class="v">{_fmt_num(sum(r["shares"] for r in rows) / _YI, 0)}</div><div class="t">份额总数（亿份）</div></div>
</div>

<div class="table-wrap">
<table id="tbl">
<thead><tr>
  <th class="sortable" data-k="index_code" data-t="str">指数代码 <span class="arrow"></span></th>
  <th class="sortable" data-k="index_name" data-t="str">指数名称 <span class="arrow"></span></th>
  <th class="sortable num" data-k="etf_count" data-t="num">ETF数量 <span class="arrow"></span></th>
  <th class="sortable num" data-k="shares" data-t="num">份额总数(亿份) <span class="arrow"></span></th>
  {ths}
</tr></thead>
<tbody>
{chr(10).join(body)}
</tbody>
</table>
</div>

<div class="note">
数据口径：份额与历史基准均取自<b>交易所官方披露</b>——沪市用上交所「ETF基金份额」按日数据（可回溯历史），
深市用深交所「基金规模」日频数据（单次可查近半年），更早历史由天天基金 F10「规模变动」季度末总份额补足；
仅交易所报表未覆盖的个别代码用行情（总市值÷最新价）兜底。每个单元格下方标注基期日期，悬停可见偏差天数。
份额增加标红、减少标绿，点击表头可排序。
数据来源：上海证券交易所 / 深圳证券交易所 / 天天基金 F10 / 腾讯财经行情；仅供参考，不构成投资建议。
</div>
<div class="note">
指数归属口径：跟踪指数代码按以下优先级确定 —— ①深市取深交所「拟合指数」官方映射；
②按跟踪标的名称匹配<b>中证指数官网（约 3000 条）</b>与<b>国证指数官网（约 1480 条）</b>的全量指数清单（全称/简称/归一化）；
③东方财富基金详情接口给出的官方跟踪指数代码（覆盖标普/MSCI/富时/恒生/中债等境外与债券指数）；
④本地人工别名 data/index_alias.json。同名但不同机构发布的指数（如「新能电池」同为国证 980032 与中证 931555）不会互相覆盖。
</div>
{un_block}

<script>
(function() {{
  var tbl = document.getElementById("tbl");
  var tb = tbl.tBodies[0];
  var ths = tbl.tHead.rows[0].cells;
  // 每行数据预提取到 dataset
  var rows = Array.prototype.slice.call(tb.rows);
  rows.forEach(function(tr) {{
    var d = {{}};
    for (var i = 0; i < ths.length; i++) {{
      var th = ths[i], k = th.dataset.k;
      var txt = tr.cells[i].innerText.trim();
      d[k] = (th.dataset.t === "num") ? parseFloat(txt.replace(/[%,亿份]/g, "")) || -Infinity : txt;
    }}
    tr.dataset.v = JSON.stringify(d);
  }});
  function sort(k, t, dir) {{
    rows.sort(function(a, b) {{
      var va = JSON.parse(a.dataset.v)[k], vb = JSON.parse(b.dataset.v)[k];
      if (t === "num") return (va - vb) * dir;
      return String(va).localeCompare(String(vb), "zh") * dir;
    }});
    rows.forEach(function(tr) {{ tb.appendChild(tr); }});
  }}
  var curKey = "1m", curDir = -1;
  function paint() {{
    for (var i = 0; i < ths.length; i++) {{
      var ar = ths[i].querySelector(".arrow");
      if (ar) ar.textContent = (ths[i].dataset.k === curKey) ? (curDir > 0 ? "▲" : "▼") : "";
    }}
  }}
  for (var i = 0; i < ths.length; i++) {{
    (function(th) {{
      th.addEventListener("click", function() {{
        var k = th.dataset.k, t = th.dataset.t;
        if (k === curKey) curDir = -curDir; else {{ curKey = k; curDir = (t === "num") ? -1 : 1; }}
        sort(k, t, curDir); paint();
      }});
    }})(ths[i]);
  }}
  sort(curKey, "num", curDir); paint();
}})();
</script>
</body>
</html>"""
    return doc


def run_report():
    etf_changes = compute_etf_changes()
    rows = aggregate_by_index(etf_changes)
    unmatched = [(c, v) for c, v in etf_changes.items() if not v["index_code"]]
    cur_dates = [v["cur_date"] for v in etf_changes.values()]
    cur_date = max(cur_dates) if cur_dates else "—"
    doc = render_html(rows, unmatched, len(etf_changes), cur_date)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"报告已生成: {REPORT_PATH}（指数维度 {len(rows)} 个，覆盖 ETF {len(etf_changes)} 只，"
          f"未识别跟踪指数 {len(unmatched)} 只）")
    return REPORT_PATH


if __name__ == "__main__":
    storage.init_db()
    run_report()
