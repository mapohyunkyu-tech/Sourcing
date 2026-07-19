# -*- coding: utf-8 -*-
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Dict

APP_DIR = Path.home() / ".marketscout"
SETTINGS_FILE = APP_DIR / "settings.json"

DEFAULTS: Dict[str, Any] = {
    "auth_mode": "hub",
    "client_id": "",
    "client_secret": "",
    "target_month": 8,
    "top_n": 50,
    "exclude_low_confidence": False,
    "sort_by": "추천순",
}

def load_settings() -> Dict[str, Any]:
    data = DEFAULTS.copy()
    try:
        if SETTINGS_FILE.exists():
            data.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return data

def save_settings(values: Dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)
    try:
        os.chmod(SETTINGS_FILE, 0o600)
    except OSError:
        pass

def delete_credentials() -> None:
    data = load_settings()
    data["client_id"] = ""
    data["client_secret"] = ""
    save_settings(data)
