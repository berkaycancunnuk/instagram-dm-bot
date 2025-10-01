# integrations/sinks.py
import os
import json
import logging
import requests

logger = logging.getLogger("igbot")

SHEETS_WEBHOOK_URL = os.getenv("SHEETS_WEBHOOK_URL")
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET")

# Ayrı session ve timeout: yeniden kullanım + daha hızlı
_SESSION = requests.Session()
_TIMEOUT = (5, 10)  # (connect, read) saniye

def post_lead_to_sheets(user_id: str, brand: str, source: str, lead_dict: dict) -> dict:
    """
    Google Apps Script (Web App) uç noktasına lead kaydı gönderir.
    Env:
      - SHEETS_WEBHOOK_URL = https://script.google.com/macros/s/.../exec
      - WEBHOOK_SECRET     = server-side shared secret
    """
    if not SHEETS_WEBHOOK_URL:
        logger.warning("sheets_skip: SHEETS_WEBHOOK_URL empty")
        return {"ok": False, "skip": "no url"}

    payload = {
        "user_id": user_id,
        "brand"  : brand,
        "source" : source,
        "lead"   : lead_dict or {},
        "secret" : WEBHOOK_SECRET or "",
    }

    try:
        resp = _SESSION.post(SHEETS_WEBHOOK_URL, json=payload, timeout=_TIMEOUT)
        # Yanıtı logla (kısaltıp)
        body_snip = (resp.text or "")[:300].replace("\n", " ")
        logger.info(f"sheets_post status={resp.status_code} body_snip={body_snip}")
        return {"ok": resp.status_code == 200, "status": resp.status_code, "body": body_snip}
    except Exception as e:
        logger.exception(f"sheets_exc {e}")
        return {"ok": False, "error": repr(e)}