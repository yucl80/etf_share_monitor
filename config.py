# -*- coding: utf-8 -*-
"""全局配置：路径、数据源、统计窗口等。"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
DB_PATH = os.path.join(DATA_DIR, "etf_shares.db")
REPORT_PATH = os.path.join(OUTPUT_DIR, "etf_index_share_report.html")

for _d in (DATA_DIR, OUTPUT_DIR):
    os.makedirs(_d, exist_ok=True)

HTTP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://fundf10.eastmoney.com/",
}
QUOTE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://gu.qq.com/",
}
# 交易所官方接口请求头（份额数据权威来源）
SSE_HEADERS = {
    "User-Agent": HTTP_HEADERS["User-Agent"],
    "Referer": "https://www.sse.com.cn/",
}
SZSE_SCALE_HEADERS = {
    "User-Agent": HTTP_HEADERS["User-Agent"],
    "Referer": "https://www.szse.cn/market/fund/volume/etf/index.html",
}
SZSE_LIST_HEADERS = {
    "User-Agent": HTTP_HEADERS["User-Agent"],
    "Referer": "https://www.szse.cn/market/fund/etf/index.html",
}
# 东方财富基金详情接口（直接返回跟踪指数代码 INDEXCODE，权威）
FUND_INDEX_HEADERS = {
    "User-Agent": HTTP_HEADERS["User-Agent"],
    "Referer": "https://fund.eastmoney.com/",
}

# 深交所日频规模接口一次最多查询 6 个月，留出余量
SZSE_HISTORY_DAYS = 180
# 上交所日频接口按单日查询，为目标日期向前回溯的最大天数（用于跳过周末/休市日）
SSE_PROBE_BACK_DAYS = 12

TIMEOUT = 12          # 单请求超时（秒）
RETRIES = 2           # 单请求重试次数
F10_WORKERS = 6       # F10 并发抓取线程数
QUOTE_BATCH = 50      # 腾讯行情单批请求的证券数量
META_REFRESH_DAYS = 30   # 基金概况（跟踪指数）缓存有效期
GMBD_REFRESH_DAYS = 7    # F10 规模变动（历史份额）补全频率
INDEX_DICT_REFRESH_DAYS = 30  # 官方指数字典（中证/国证官网全量清单）缓存天数
INDEX_RESOLVE_WORKERS = 8     # 跟踪指数解析（基金详情接口）并发数
FUND_INDEX_MIN_INTERVAL = 0.4  # 基金详情接口最小请求间隔（秒）——该接口限流较严
FUND_INDEX_RETRIES = 4         # 基金详情接口遇「网络繁忙」时的重试次数

# 变化窗口（天）-> 允许使用的最近历史记录距目标日期的最大偏差（天）
# 历史份额来源有两类：
#   quote : 每次运行写入的行情快照（日频，随运行逐步积累）
#   gmbd  : 官网 F10「规模变动」披露的季度末总份额（官方最细粒度历史数据）
WINDOWS = [
    ("1d", "最近1日", 1, 4),
    ("1w", "最近1周", 7, 6),
    ("1m", "最近1月", 30, 20),
    ("3m", "最近3月", 91, 45),
    ("6m", "最近6月", 182, 60),
]
