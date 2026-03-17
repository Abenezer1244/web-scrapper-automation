---
description: Rules for data export logic
globs: src/utils/data_exporter.py
---

# Export Rules

- Always use `DataExporter.export()` as the single entry point — do not call `to_csv`, `to_json`, or `to_excel` directly from outside this class
- All exports must be timestamped via `_generate_filename()`
- Output always goes to `Settings.EXPORTS_DIR` — never write exports to arbitrary paths
- Supported formats: `csv`, `json`, `excel` — do not add new formats without updating `export()`
- Input data must be `List[Dict[str, Any]]` — validate upstream before passing to exporter
- Never silence export errors; always re-raise after logging
