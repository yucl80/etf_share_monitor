# -*- coding: utf-8 -*-
"""A股ETF份额变化监控 —— 入口

日常使用（默认）：直接运行。数据今天已抓过就跳过抓取；报告是最新的就直接打开，
否则先抓数据 / 生成报告，最后自动打开报告。

用法:
    python main.py              # 智能运行：跳过重复抓取 → 按需生成报告 → 打开报告
    python main.py update       # 强制抓取最新数据并重建报告
    python main.py report       # 仅用本地数据重建报告（不打开）
    python main.py index        # 仅修复跟踪指数映射（补缺失 + 纠正错误）
    python main.py open         # 仅打开已有报告（不抓取、不重建）

选项:
    -f, --force    忽略「今日已成功抓取」标记，强制重新抓取数据
    --no-open      完成后不自动打开报告

「今日不再重复抓取」的判定（满足其一即跳过网络抓取）：
    1. 今日已成功抓取，且本地快照已包含今日数据；
    2. 今日为非工作日；
    3. 当前尚未到交易所份额日报发布时点（config.DATA_READY_HOUR，默认 18 点）；
    4. 今日已在份额日报发布时点之后抓取过。
若上次抓取发生在发布时点之前（例如上午运行过一次），当晚再运行会自动补抓一次。
"""
import os
import sys
from datetime import datetime

import storage
from config import DATA_READY_HOUR, REPORT_PATH


def _parse_args(argv):
    force = any(a in ("-f", "--force") for a in argv)
    no_open = "--no-open" in argv
    args = [a for a in argv if not a.startswith("-")]
    return (args[0] if args else "all"), force, no_open


def _skip_fetch_reason():
    """判断今日是否可以跳过网络抓取，返回 (是否跳过, 原因)。"""
    st = storage.get_states()
    if st.get("last_fetch_date") != storage.today_str():
        return False, "今日尚未抓取"
    if st.get("last_fetch_ok") != "1":
        return False, "上次抓取核心数据未成功"
    latest = storage.latest_share_date() or ""
    if latest >= storage.today_str():
        return True, f"今日份额快照已在本地（{latest}）"
    now = datetime.now()
    if now.weekday() >= 5:
        return True, "今日为非工作日，无新增份额数据"
    if now.hour < DATA_READY_HOUR:
        return True, f"当日份额日报通常 {DATA_READY_HOUR}:00 后才发布，暂无需重抓"
    last = st.get("last_fetch_time") or ""
    if len(last) >= 13 and last[11:13].isdigit() and int(last[11:13]) >= DATA_READY_HOUR:
        return True, "今日已在份额日报发布后抓取过"
    return False, "上次抓取早于份额日报发布时点，补抓一次"


def _open_report(path):
    """用系统默认程序打开报告（Windows 走 os.startfile）。"""
    try:
        if hasattr(os, "startfile"):
            os.startfile(path)                       # noqa: S606  Windows 专用
        else:
            import urllib.parse
            import webbrowser
            webbrowser.open("file://" + urllib.parse.quote(path))
        print(f"已打开报告: {path}")
    except Exception as e:  # noqa: BLE001
        print(f"  ! 自动打开失败（{e}）；请手动打开: {path}")


def main():
    cmd, force, no_open = _parse_args(sys.argv[1:])
    storage.init_db()

    if cmd == "index":
        import updater
        updater.fix_index_mappings()
        return

    if cmd == "open":
        if os.path.exists(REPORT_PATH):
            _open_report(REPORT_PATH)
        else:
            print(f"报告不存在: {REPORT_PATH}（请先运行 python main.py 生成）")
        return

    if cmd == "update":
        import report
        import updater
        print("=== 强制更新数据 + 重建报告 ===")
        updater.run_update()
        report.run_report()
        if not no_open:
            _open_report(REPORT_PATH)
        return

    if cmd == "report":
        import report
        report.run_report()
        return

    # ---- 默认 / all：智能流程 ----
    if force:
        skip, reason = False, "指定了 --force，忽略「今日已抓取」标记"
    else:
        skip, reason = _skip_fetch_reason()
    if skip:
        print(f"[跳过数据抓取] {reason}")
    else:
        import updater
        print(f"[抓取数据] {reason}")
        updater.run_update()

    import report
    need, why = report.report_needs_rebuild()
    if need:
        print(f"[生成报告] {why}")
        report.run_report()
    else:
        print(f"[复用报告] {why}")

    if not no_open:
        _open_report(REPORT_PATH)


if __name__ == "__main__":
    main()
