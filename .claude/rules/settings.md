---
description: Rules for configuration and environment settings
globs: src/config/settings.py
---

# Settings Rules

- All configuration must come from environment variables via `os.getenv()` — no hardcoded values
- Always provide a sensible default in `os.getenv('KEY', 'default')`
- New settings must be added to both `settings.py` and `.env.example`
- Never read `.env` or call `load_dotenv()` anywhere except `settings.py`
- Path constants (`BASE_DIR`, `EXPORTS_DIR`, etc.) must use `pathlib.Path`, not string concatenation
- `ensure_dirs()` must be called at scraper initialization — do not call it in `settings.py` at import time
