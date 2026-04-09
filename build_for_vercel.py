from __future__ import annotations

from pathlib import Path

from build_shuttle_webapp import build_webapp, is_monthly_schedule_workbook, monthly_workbook_sort_key


def main() -> None:
    root = Path(__file__).resolve().parent
    monthly_files = [
        path
        for path in sorted(root.glob("*.xlsx"), key=monthly_workbook_sort_key)
        if is_monthly_schedule_workbook(path)
    ]
    if not monthly_files:
        raise SystemExit("No monthly shuttle workbook found. Expected files like '등송영표 4월.xlsx'.")

    latest = monthly_files[-1]
    output = root / "webapp" / "index.html"
    build_webapp(latest, output)
    print(output)


if __name__ == "__main__":
    main()
