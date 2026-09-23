# -*- coding: utf-8 -*-
"""数据源封装（均为公开官网/官方行情接口）：

1. ETF 全量名单        : 天天基金 fundcode_search.js（按场内代码规则过滤）
2. 【沪市】官方日频份额 : 上交所 query.sse.com.cn，ETFGM 报表按 STAT_DATE 单日查询（可回溯历史）
3. 【深市】官方日频份额 : 深交所 scsj_fund_jjgm 报表，按日期区间导出 xlsx（单次最长 6 个月）
4. 【深市】拟合指数映射 : 深交所 ETF 列表（CATALOGID=1945）含「拟合指数」= 指数代码 + 名称
5. 兜底份额快照        : 腾讯财经行情（总市值/最新价 -> 份额），仅用于交易所报表未覆盖的代码
6. 历史份额(季度)      : 天天基金 F10「规模变动」期末总份额（用于补足更早的历史）
7. 跟踪指数名称        : 天天基金 F10「基金概况」跟踪标的
8. 指数名称->代码      : 官方指数字典（index_dict，中证/国证官网全量清单）
                        + 东方财富搜索适配接口（中证系指数类别码为 "24"）
9. 跟踪指数代码(权威)  : 东方财富基金详情接口 INDEXCODE（覆盖境外/债券/主题指数）
"""
import difflib
import io
import json
import os
import random
import re
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

from config import (FUND_INDEX_HEADERS, FUND_INDEX_MIN_INTERVAL, FUND_INDEX_RETRIES,
                    HTTP_HEADERS, QUOTE_HEADERS, RETRIES, SSE_HEADERS,
                    SSE_PROBE_BACK_DAYS, SZSE_HISTORY_DAYS, SZSE_LIST_HEADERS,
                    SZSE_SCALE_HEADERS, TIMEOUT)

_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_GMBD_ROW = re.compile(
    r"<tr><td>(\d{4}-\d{2}-\d{2})</td>\s*"
    r"<td class='tor'>([^<]*)</td>\s*"
    r"<td class='tor'>([^<]*)</td>\s*"
    r"<td class='tor'>([^<]*)</td>\s*"
    r"<td class='tor'>([^<]*)</td>\s*"
    r"<td class='tor'>([^<]*)</td></tr>"
)


def http_get(url, headers=None):
    last_err = None
    for i in range(RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=headers or HTTP_HEADERS)
            return urllib.request.urlopen(req, timeout=TIMEOUT).read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i < RETRIES:
                time.sleep(1.0 + i)
    raise last_err


def http_get_bytes(url, headers=None, timeout=None):
    """下载二进制内容（交易所 xlsx 报表）。"""
    last_err = None
    for i in range(RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=headers or HTTP_HEADERS)
            return urllib.request.urlopen(req, timeout=timeout or TIMEOUT).read()
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i < RETRIES:
                time.sleep(1.0 + i)
    raise last_err


def http_post_bytes(url, payload, headers=None, timeout=None):
    """以 JSON body 发起 POST 并返回二进制内容（中证指数官网导出接口）。"""
    last_err = None
    body = json.dumps(payload).encode("utf-8")
    for i in range(RETRIES + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=headers or HTTP_HEADERS,
                                         method="POST")
            return urllib.request.urlopen(req, timeout=timeout or TIMEOUT).read()
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i < RETRIES:
                time.sleep(1.0 + i)
    raise last_err


# ---------------------------------------------------------------- xlsx 解析（无第三方依赖）
def _parse_xlsx_rows(raw):
    """把 xlsx 二进制解析为二维字符串表（支持 inlineStr / sharedStrings / 数值）。"""
    zf = zipfile.ZipFile(io.BytesIO(raw))
    shared = []
    if "xl/sharedStrings.xml" in zf.namelist():
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        shared = ["".join(t.text or "" for t in si.iter(_XLSX_NS + "t")) for si in root.iter(_XLSX_NS + "si")]
    sheet = next(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet"))
    root = ET.fromstring(zf.read(sheet))
    rows = []
    for row in root.iter(_XLSX_NS + "row"):
        vals = []
        for cell in row.iter(_XLSX_NS + "c"):
            ctype, vnode, inode = cell.get("t"), cell.find(_XLSX_NS + "v"), cell.find(_XLSX_NS + "is")
            if ctype == "inlineStr" and inode is not None:
                vals.append("".join(t.text or "" for t in inode.iter(_XLSX_NS + "t")).strip())
            elif ctype == "s" and vnode is not None:
                vals.append(shared[int(vnode.text)].strip())
            elif vnode is not None:
                vals.append((vnode.text or "").strip())
            else:
                vals.append("")
        rows.append(vals)
    return rows



# ---------------------------------------------------------------- ETF 名单
def fetch_etf_list():
    """全市场场内 ETF 名单，返回 [(code, name), ...]"""
    text = http_get("https://fund.eastmoney.com/js/fundcode_search.js")
    arr = json.loads(text[text.index("["): text.rindex("]") + 1])
    out = []
    for code, _, name, ftype, _ in arr:
        if len(code) != 6:
            continue
        if not (code.startswith("5") or code.startswith(("15", "16", "18"))):
            continue  # 仅保留场内代码段：沪 5xxxxx / 深 15,16,18xxxx
        if "ETF" not in name:
            continue
        if any(k in name for k in ("联接", "FOF")):
            continue
        if ftype and "货币" in ftype:
            continue  # 场内货币基金无跟踪指数，不纳入
        out.append((code, name))
    # 去重
    seen, result = set(), []
    for code, name in out:
        if code not in seen:
            seen.add(code)
            result.append((code, name))
    return result


# ---------------------------------------------------------------- 行情快照
def _tencent_prefix(code):
    return "sh" if code.startswith("5") else "sz"


def fetch_quotes(codes, batch_size=50):
    """批量行情 -> {code: {"date": "YYYY-MM-DD", "shares": 份额, "price": 价格}}

    份额 = 总市值(元) / 最新价(元)。停牌/无价证券跳过。
    """
    out = {}
    for i in range(0, len(codes), batch_size):
        batch = codes[i: i + batch_size]
        q = ",".join(_tencent_prefix(c) + c for c in batch)
        text = http_get("https://qt.gtimg.cn/q=" + q, headers=QUOTE_HEADERS)
        for m in re.finditer(r'v_(?:sh|sz)(\d{6})="([^"]*)"', text):
            code, payload = m.group(1), m.group(2).split("~")
            try:
                price = float(payload[3])
                mktcap = float(payload[45])   # 总市值（亿元）
                ts = payload[30]              # 行情时间 yyyymmddHHMMSS
                if price <= 0 or mktcap <= 0 or len(ts) < 8:
                    continue
                date = datetime.strptime(ts[:8], "%Y%m%d").strftime("%Y-%m-%d")
                out[code] = {"date": date, "shares": mktcap * 1e8 / price, "price": price}
            except (ValueError, IndexError):
                continue
        time.sleep(0.2)
    return out


# ---------------------------------------------------------------- F10：概况 + 规模变动
def fetch_index_name(code):
    """基金概况页 -> 跟踪标的名称（如 沪深300指数）。"""
    html = http_get(f"https://fundf10.eastmoney.com/jbgk_{code}.html")
    m = re.search(r"跟踪标的</th><td>([^<]+)</td>", html)
    name = m.group(1).strip() if m else None
    if name and name in ("", "无"):
        return None
    return name


def fetch_gmbd_shares(code):
    """F10 规模变动 -> [(季度末日期, 期末总份额(份)), ...] 按日期升序。"""
    text = http_get(
        f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=gmbd&code={code}&rt={time.time():.3f}"
    )
    rows = []
    for m in _GMBD_ROW.finditer(text):
        date, _, _, end_shares, _, _ = (g.strip() for g in m.groups())
        try:
            shares_yi = float(end_shares.replace(",", ""))
        except ValueError:
            continue
        if shares_yi <= 0:
            continue
        rows.append((date, shares_yi * 1e8))  # 亿份 -> 份
    rows.sort(key=lambda x: x[0])
    return rows


# ---------------------------------------------------------------- 跟踪指数代码（权威）
_fund_index_lock = __import__("threading").Lock()
_fund_index_last = [0.0]


def fetch_fund_index(code):
    """东方财富基金详情接口 -> (跟踪指数代码, 跟踪指数名称)。

    这是最权威的兜底来源：接口直接给出 INDEXCODE / INDEXNAME，
    可覆盖中证/国证细分主题指数，以及标普/MSCI/富时/恒生/中债等境外与债券指数。
    例：513030 -> (GDAXI, 法兰克福DAX指数)；511090 -> (CBA21801, 中债-30年期国债财富(总值)指数)。

    注意：该接口限流较严（ErrCode=61136403「网络繁忙」），因此内置
    全局限速（FUND_INDEX_MIN_INTERVAL）+ 指数退避重试。
    """
    url = ("https://fundmobapi.eastmoney.com/FundMNewApi/FundMNBasicInformation"
           f"?FCODE={code}&deviceid=1&plat=Iphone&product=EFund&version=1")
    last_err = None
    for attempt in range(FUND_INDEX_RETRIES):
        with _fund_index_lock:                      # 全局限速：多线程下保持请求间隔
            gap = FUND_INDEX_MIN_INTERVAL - (time.time() - _fund_index_last[0])
            if gap > 0:
                time.sleep(gap)
            _fund_index_last[0] = time.time()
        try:                                        # 单次请求（内部不自带重试，避免请求放大）
            req = urllib.request.Request(url, headers=FUND_INDEX_HEADERS)
            data = json.loads(urllib.request.urlopen(req, timeout=TIMEOUT).read().decode("utf-8", "ignore"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
            continue
        err_code = data.get("ErrCode")
        if data.get("Success") is False and err_code not in (0, None):
            last_err = RuntimeError(f"ErrCode={err_code} {data.get('ErrMsg') or ''}")
            time.sleep(2.0 * (attempt + 1))           # 限流退避
            continue
        info = data.get("Datas") or {}
        index_code = (info.get("INDEXCODE") or "").strip()
        index_name = (info.get("INDEXNAME") or "").strip()
        return (index_code or None), (index_name or None)
    raise last_err if last_err else RuntimeError("基金详情接口无响应")


# ---------------------------------------------------------------- 指数名称 -> 指数代码
_STRIP_TOKENS = ("成份指数", "成份", "指数", "全收益", "净收益", "收益率", "(价格)", "（价格）",
                 "(人民币)", "（人民币）", "价格")
_STRIP_PREFIX = ("中证全指", "中证", "上证", "深证", "国证", "标普", "全指")
_ACCEPT_CLASS = {"Index": 0, "24": 0, "NDI": 1, "HK": 2, "UniversalIndex": 3, "SGE": 4}
_MIN_RATIO = 0.6


def _index_candidates(index_name):
    """生成由精确到宽松的检索关键词序列。"""
    cands = []

    def add(s):
        s = re.sub(r"\s+", "", s or "")
        if s and s not in cands:
            cands.append(s)

    base = re.sub(r"[（(].*?[)）]", "", index_name).strip()  # 去括号
    add(index_name)
    add(base)
    for tok in _STRIP_TOKENS:
        b = base
        while b.endswith(tok) and len(b) > len(tok):
            b = b[: -len(tok)]
            add(b)
    for b in list(cands):
        for p in _STRIP_PREFIX:
            if b.startswith(p) and len(b) > len(p) + 1:
                add(b[len(p):])
    for b in list(cands):
        add(b.replace("科创板", "科创").replace("创业板", "创业"))
    return cands


def _search_items(keyword):
    url = ("https://searchadapter.eastmoney.com/api/suggest/get?input="
           + urllib.parse.quote(keyword) + "&type=14&count=15")
    data = json.loads(http_get(url, headers=QUOTE_HEADERS))
    return (data.get("QuotationCodeTable") or {}).get("Data") or []


def _load_alias():
    """本地手工别名映射（data/index_alias.json）：{"跟踪标的名称": "指数代码"}。

    公开检索库覆盖不全时（部分中证/国证细分主题指数检索不到），
    可在此文件中手工补充，优先级最高。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "index_alias.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items()
                if isinstance(v, str) and v and not k.startswith("_")}
    except Exception:  # noqa: BLE001
        return {}


_ALIAS = _load_alias()
_RESOLVE_CACHE = {}          # 名称 -> 代码（进程内缓存，避免同一名称反复走网络）
_RESOLVE_LOCK = __import__("threading").Lock()


def resolve_index_code(index_name, cache=None):
    """把跟踪标的名称解析为指数代码（只返回代码）。"""
    return resolve_index_code_with_source(index_name, cache)[0]


def resolve_index_code_with_source(index_name, cache=None):
    """把跟踪标的名称解析为指数代码，并返回来源。

    例：沪深300指数 -> 000300；创业板指数(价格) -> 399006；黄金9999 -> AU9999；
        恒生指数 -> HSI；中证港股通高股息投资港元指数 -> 930914。

    解析优先级（返回值 source 对应来源）：
      1. 'alias'  本地手工别名 data/index_alias.json（人工 override，最高优先级）
      2. 'dict'   官方指数字典（index_dict：中证指数官网 + 国证指数官网全量清单）
                  —— 解决公开检索库对中证/国证细分主题指数覆盖不全的问题
      3. 'search' 东方财富搜索适配接口（多级关键词回退 + 名称相似度）
    仍解析不到时返回 (None, None)，由 updater 用基金详情接口 INDEXCODE 兜底。
    """
    if not index_name:
        return None, None
    if cache is not None and index_name in cache:
        return cache[index_name], None
    with _RESOLVE_LOCK:
        if index_name in _RESOLVE_CACHE:
            hit = _RESOLVE_CACHE[index_name]
            if cache is not None:
                cache[index_name] = hit[0]
            return hit
    if index_name in _ALIAS:
        hit = (_ALIAS[index_name], "alias")
        with _RESOLVE_LOCK:
            _RESOLVE_CACHE[index_name] = hit
        if cache is not None:
            cache[index_name] = hit[0]
        return hit

    code, source = None, None
    try:                                   # 2. 官方指数字典（中证/国证官网全量清单）
        import index_dict
        code, _hit = index_dict.lookup_official(index_name)
        if code:
            source = "dict"
    except Exception as e:  # noqa: BLE001
        print(f"  ! 指数字典不可用({index_name}): {e}")

    if not code:                           # 3. 东方财富搜索适配接口
        code = _search_index_code(index_name)
        source = "search" if code else None

    with _RESOLVE_LOCK:
        _RESOLVE_CACHE[index_name] = (code, source)
    if cache is not None:
        cache[index_name] = code
    return code, source


def _search_index_code(index_name):
    """东方财富搜索适配接口解析（多级关键词回退 + 名称相似度匹配）。"""
    best = None  # (ratio, class_priority, code)
    for q in _index_candidates(index_name):
        if len(q) < 2:
            continue
        try:
            items = _search_items(q)
        except Exception:  # noqa: BLE001
            continue
        for it in items:
            cls = it.get("Classify")
            if cls not in _ACCEPT_CLASS or not it.get("Code"):
                continue
            name = re.sub(r"[（(].*?[)）]", "", it.get("Name") or "").strip()
            ratio = difflib.SequenceMatcher(None, q, name).ratio()
            if ratio < _MIN_RATIO:
                continue
            key = (round(ratio, 3), -_ACCEPT_CLASS[cls])
            if best is None or key > (best[0], best[1]):
                best = (key[0], key[1], it["Code"])
        if best and best[0] >= 0.95:  # 已有高置信匹配，无需继续放宽关键词
            break
    return best[2] if best else None


# ================================================================ 交易所官方日频份额
_sse_day_cache = {}   # "YYYY-MM-DD" -> {code: shares}（同日只请求一次）


def fetch_sse_shares(date_str):
    """上交所官网 ETF 基金份额（单日，沪市全量）。

    一次请求返回当日全部沪市 ETF 的「基金份额」（单位：万份，已换算为份）。
    非交易日返回 {}。参考页面：https://www.sse.com.cn/assortment/fund/etf/list/scale/
    """
    if date_str in _sse_day_cache:
        return _sse_day_cache[date_str]
    params = {
        "isPagination": "true", "pageHelp.pageSize": "10000", "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1", "pageHelp.cacheSize": "1", "pageHelp.endPage": "1",
        "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L", "STAT_DATE": date_str,
    }
    url = "https://query.sse.com.cn/commonQuery.do?" + urllib.parse.urlencode(params)
    out = {}
    try:
        data = json.loads(http_get(url, headers=SSE_HEADERS))
        for row in (data.get("result") or []):
            code = str(row.get("SEC_CODE") or "").strip()
            raw = str(row.get("TOT_VOL") or "").replace(",", "").strip()
            try:
                shares = float(raw) * 1e4          # 万份 -> 份
            except ValueError:
                continue
            if code and shares > 0:
                out[code] = shares
    except Exception:  # noqa: BLE001
        out = {}
    _sse_day_cache[date_str] = out
    return out


def fetch_sse_nearest(target_date, max_back=None):
    """从目标日期起向前回溯，返回最近一个有数据的交易日及其份额。

    返回 (实际日期 "YYYY-MM-DD" 或 None, {code: shares})。
    """
    max_back = max_back or SSE_PROBE_BACK_DAYS
    day = datetime.strptime(target_date, "%Y-%m-%d")
    for i in range(max_back + 1):
        d = (day - timedelta(days=i)).strftime("%Y-%m-%d")
        data = fetch_sse_shares(d)
        if data:
            return d, data
    return None, {}


def fetch_szse_scale(start_date, end_date):
    """深交所官网基金规模日频数据（区间查询，单次最长 6 个月）。

    返回 [(date, code, name, shares), ...]，份额单位为「份」。
    参考页面：https://www.szse.cn/market/fund/volume/etf/index.html
    """
    params = {
        "SHOWTYPE": "xlsx", "CATALOGID": "scsj_fund_jjgm", "TABKEY": "tab1",
        "txtStart": start_date, "txtEnd": end_date, "jjlb": "ETF", "random": str(random.random()),
    }
    url = "https://www.szse.cn/api/report/ShowReport?" + urllib.parse.urlencode(params)
    raw = http_get_bytes(url, headers=SZSE_SCALE_HEADERS, timeout=120)
    try:
        rows = _parse_xlsx_rows(raw)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for row in rows[1:]:
        if len(row) < 4:
            continue
        date, code, name, shares_s = row[0].strip(), row[1].strip(), row[2].strip(), row[3].replace(",", "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not code:
            continue
        try:
            shares = float(shares_s)
        except ValueError:
            continue
        if shares > 0:
            out.append((date, code, name, shares))
    return out


def fetch_szse_etf_list():
    """深交所 ETF 列表：代码 -> {name, index_code, index_name, shares}。

    导出列含「拟合指数」（形如 "399372 大盘成长"），可直接作为跟踪指数映射，
    同时提供「当前规模(份)」作为官方当期份额。
    """
    params = {"SHOWTYPE": "xlsx", "CATALOGID": "1945", "TABKEY": "tab1", "random": str(random.random())}
    url = "https://www.szse.cn/api/report/ShowReport?" + urllib.parse.urlencode(params)
    raw = http_get_bytes(url, headers=SZSE_LIST_HEADERS, timeout=90)
    out = {}
    try:
        rows = _parse_xlsx_rows(raw)
    except Exception:  # noqa: BLE001
        return out
    for row in rows[1:]:
        if len(row) < 4:
            continue
        code, name, fit_index = row[0].strip(), row[1].strip(), row[2].strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        shares = None
        try:
            shares = float(row[3].replace(",", "").strip())
        except ValueError:
            pass
        m = re.match(r"^([A-Za-z0-9]{4,8})\s*(.*)$", fit_index)
        out[code] = {
            "name": name,
            "index_code": m.group(1) if m else None,
            "index_name": (m.group(2).strip() or None) if m else None,
            "shares": shares,
        }
    return out


def szse_history_window(days=None):
    """深交所日频份额查询的日期窗口（不超过 6 个月）。"""
    days = days or SZSE_HISTORY_DAYS
    end = datetime.now()
    start = end - timedelta(days=days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
