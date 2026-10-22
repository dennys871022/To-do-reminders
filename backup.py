"""
backup.py
----------
把雲端試算表的所有分頁各匯出成一個 CSV 檔，放進 backups/日期/ 資料夾。
搭配 .github/workflows/backup.yml 每週自動執行，備份檔會以「Artifact」的形式保存在
GitHub Actions 的執行紀錄裡（預設保留 90 天），不會進到你的程式碼儲存庫。
"""

import csv
import os
import sys
from datetime import datetime, timedelta, timezone

import data_store


def main():
    ss = data_store._open_spreadsheet()
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    out_dir = os.path.join("backups", stamp)
    os.makedirs(out_dir, exist_ok=True)

    count = 0
    for ws in ss.worksheets():
        values = ws.get_all_values()
        path = os.path.join(out_dir, f"{ws.title}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig：Excel 直接開不會亂碼
            csv.writer(f).writerows(values)
        print(f"已匯出 {ws.title}：{max(len(values) - 1, 0)} 列")
        count += 1

    if count == 0:
        print("沒有任何分頁可以備份")
        sys.exit(1)
    print(f"備份完成，共 {count} 個分頁 → {out_dir}")


if __name__ == "__main__":
    main()
