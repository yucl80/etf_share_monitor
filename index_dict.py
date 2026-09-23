# -*- coding: utf-8 -*-
"""官方指数全量字典（中证指数官网 + 国证指数官网）。

背景：东方财富检索适配接口对中证/国证**细分主题指数**覆盖不全
（例如「中证电池主题指数」「上证科创板综合指数」「国证航天航空行业指数」
「中证港股通高股息投资港元指数」都检索不到），导致跟踪指数解析失败。
本模块直接抓取两家指数官网的全量指数清单，在本地做名称匹配。

数据源（均为官网公开接口）：
* 中证指数官网
    POST https://www.csindex.com.cn/csindex-home/exportExcel/indexAll/CH
    返回 xlsx，含 指数代码 / 指数简称 / 指数全称；indexSeries=None 时覆盖
    中证系列 + 上证系列，约 3000 条。
* 国证指数官网
    GET  https://www.cnindex.com.cn/index/indexList?channelCode=-1&rows=5000&pageNum=1
    返回 JSON，含 indexcode / indexname（简称）/ indexfullcname（全称），约 1480 条。

字典缓存在 data/index_dict.json，INDEX_DICT_REFRESH_DAYS 天内不重复下载。
"""
import json
import os
import re
from datetime import datetime

from config import DATA_DIR, HTTP_HEADERS, INDEX_DICT_REFRESH_DAYS
from datasource import http_get, http_post_bytes, _parse_xlsx_rows

_DICT_PATH = os.path.join(DATA_DIR, "index_dict.json")

_CS_EXPORT = "https://www.csindex.com.cn/csindex-home/exportExcel/indexAll/CH"
_CS_PAYLOAD = {
    "sorter": {"sortField": "null", "sortOrder": None},
    "pager": {"pageNum": 1, "pageSize": 10},
    "indexFilter": {
        "ifCustomized": None, "ifTracked": None, "ifWeightCapped": None,
        "indexCompliance": None, "hotSpot": None, "indexClassify": None,
        "currency": None, "region": None, "indexSeries": None, "undefined": None,
    },
}
_CS_HEADERS = {
    "User-Agent": HTTP_HEADERS["User-Agent"],
    "Content-Type": "application/json;charset=UTF-8",
    "Referer": "https://www.csindex.com.cn/",
}
_CN_URL = "https://www.cnindex.com.cn/index/indexList?channelCode=-1&rows=5000&pageNum=1"
_CN_HEADERS = {
    "User-Agent": HTTP_HEADERS["User-Agent"],
    "Referer": "https://www.cnindex.com.cn/",
}

# 币种标记：跟踪标的名称常带「港元/港币/人民币」后缀，官网全称通常不含，
# 因此归一时需要双向剔除。
_CURRENCY = ("离岸人民币", "港元", "港币", "人民币", "美元")
# 归一化时从名称尾部剥离的修饰词
_TAIL_TOKENS = ("全收益指数", "净收益指数", "全收益", "收益率", "指数", "价格", "净值")

_dict_cache = None


# ---------------------------------------------------------------- 抓取
def _fetch_csindex():
    """中证指数官网全量清单 -> (全称->代码, 简称->代码)"""
    raw = http_post_bytes(_CS_EXPORT, _CS_PAYLOAD, headers=_CS_HEADERS, timeout=90)
    if not raw[:2] == b"PK":  # 非 xlsx
        raise RuntimeError("中证指数官网返回的不是 xlsx")
    rows = _parse_xlsx_rows(raw)
    if not rows:
        raise RuntimeError("中证指数官网 xlsx 解析为空")
    hdr = rows[0]
    try:
        ci, sn, fn = hdr.index("指数代码"), hdr.index("指数简称"), hdr.index("指数全称")
    except ValueError as e:
        raise RuntimeError(f"中证指数官网 xlsx 表头变化: {hdr}") from e
    full, short = {}, {}
    for r in rows[1:]:
        if len(r) <= fn or not r[ci]:
            continue
        code = r[ci].strip().zfill(6)
        if (r[fn] or "").strip():
            full.setdefault(r[fn].strip(), code)
        if (r[sn] or "").strip():
            short.setdefault(r[sn].strip(), code)
    return full, short


def _fetch_cnindex():
    """国证指数官网全量清单 -> (全称->代码, 简称->代码)"""
    data = json.loads(http_get(_CN_URL, headers=_CN_HEADERS))
    rows = (data.get("data") or {}).get("rows") or []
    if not rows:
        raise RuntimeError("国证指数官网返回为空")
    full, short = {}, {}
    for r in rows:
        code = (r.get("indexcode") or "").strip()
        if not code:
            continue
        if (r.get("indexfullcname") or "").strip():
            full.setdefault(r["indexfullcname"].strip(), code)
        if (r.get("indexname") or "").strip():
            short.setdefault(r["indexname"].strip(), code)
    return full, short


# ---------------------------------------------------------------- 归一化
def normalize(name):
    """名称归一化：去括号/空白、去币种标记、去尾部修饰词、去非关键字。

    例：中证港股通高股息投资港元指数 -> 中证港股通高股息投资
        创业板指数(价格)               -> 创业板
        中债-7-10年政策性金融债全价(总值)指数 -> 中债-7-10年政策性金融债
    """
    s = re.sub(r"[（(][^）)]*[）)]", "", name or "")
    s = re.sub(r"\s+", "", s)
    for cur in _CURRENCY:                 # 币种可能出现在中间或尾部
        s = s.replace(cur, "")
    for tok in _TAIL_TOKENS:
        if s.endswith(tok) and len(s) > len(tok):
            s = s[: -len(tok)]
            break
    return s


def _needs_normalize(name):
    """是否需要归一化匹配（保留供诊断使用）。

    归一化匹配解决三类官方全称与「跟踪标的」名称不一致的情况：
      * 币种后缀差异：中证港股通高股息投资港元指数 -> 中证港股通高股息投资指数
      * 标点/空格差异：沪深300ESG基准指数 -> 沪深300 ESG基准指数
      * 括号修饰差异：上证5年期国债指数 -> 上证5年期国债指数(全价)
    精确匹配优先，归一化仅在精确匹配失败后作为补充。
    """
    if any(cur in name for cur in _CURRENCY):
        return True
    return bool(re.search(r"[（(]", name or ""))


# ---------------------------------------------------------------- 字典装载
def _build():
    full, short = {}, {}
    errors = []
    for tag, fn in (("中证指数官网", _fetch_csindex), ("国证指数官网", _fetch_cnindex)):
        try:
            f, s = fn()
            full.update(f)
            short.update(s)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{tag}: {e}")
    if not full:
        raise RuntimeError("指数字典构建失败 -> " + "; ".join(errors))
    return {"updated": datetime.now().strftime("%Y-%m-%d"),
            "full": full, "short": short, "errors": errors}


def _read_cache():
    if not os.path.exists(_DICT_PATH):
        return None
    try:
        with open(_DICT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def load_index_dict(force=False):
    """装载官方指数字典（带本地缓存与有效期）。"""
    global _dict_cache
    if _dict_cache is not None and not force:
        return _dict_cache
    data = _read_cache()
    if data is not None and not data.get("full"):
        data = None                      # 兼容旧版缓存结构，强制重建
    need_refresh = data is None or force
    if data is not None and not need_refresh:
        try:
            age = (datetime.now() - datetime.strptime(data["updated"], "%Y-%m-%d")).days
            need_refresh = age > INDEX_DICT_REFRESH_DAYS
        except Exception:  # noqa: BLE001
            need_refresh = False
    if need_refresh:
        try:
            data = _build()
            with open(_DICT_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            if data is None:
                raise
            print(f"  ! 指数字典刷新失败，沿用本地缓存: {e}")
    norm_full, norm_short = {}, {}
    for k, v in data["full"].items():
        norm_full.setdefault(normalize(k), v)
    for k, v in data["short"].items():
        norm_short.setdefault(normalize(k), v)
    _dict_cache = {
        "updated": data["updated"],
        "full": data["full"], "short": data["short"],
        "_norm_full": norm_full, "_norm_short": norm_short,
    }
    return _dict_cache


def lookup_official(name, full_name_only=False):
    """用官方指数字典解析「跟踪标的名称 -> 指数代码」，解析不到返回 (None, None)。

    匹配顺序：精确全称 -> 精确简称 -> 归一化全称 -> 归一化简称。
    full_name_only=True 时只用「全称」匹配——用于**纠正**已有关联，
    避免把同名不同机构的指数（如「新能电池」同为国证 980032 与中证 931555）改错。

    返回 (指数代码, 命中的官方名称)。
    """
    if not name:
        return None, None
    d = load_index_dict()
    if name in d["full"]:
        return d["full"][name], name
    if not full_name_only and name in d["short"]:
        return d["short"][name], name
    key = normalize(name)
    if key != name:
        if key in d["_norm_full"]:
            return d["_norm_full"][key], key
        if not full_name_only and key in d["_norm_short"]:
            return d["_norm_short"][key], key
    return None, None


def stats():
    d = load_index_dict()
    return {"updated": d["updated"], "全称": len(d["full"]), "简称": len(d["short"])}


if __name__ == "__main__":
    print(json.dumps(stats(), ensure_ascii=False, indent=2))
