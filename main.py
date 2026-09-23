# -*- coding: utf-8 -*-
"""A股ETF份额变化监控 —— 入口

用法:
    python main.py            # 更新数据 + 生成报告
    python main.py update     # 仅更新数据（本地缺失时自动去官网补全）
    python main.py index      # 仅修复跟踪指数映射（补缺失 + 纠正错误）
    python main.py report     # 仅根据本地数据生成 HTML 报告
"""
import sys

import storage


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    storage.init_db()
    if cmd in ("all", "update"):
        import updater
        updater.run_update()
    if cmd == "index":
        import updater
        updater.fix_index_mappings()
    if cmd in ("all", "report"):
        import report
        report.run_report()


if __name__ == "__main__":
    main()
