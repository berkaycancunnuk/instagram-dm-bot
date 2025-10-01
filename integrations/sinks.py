# integrations/sinks.py
import os, time, json, logging, requests
from typing import Dict, Any

log = logging.getLogger("igbot.sink")

SHEETS_WEBHOOK_URL = os.getenv("SHEETS_WEBHOOK_URL", "").strip()
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "").strip()

DEFAULT_TIMEOUT = (5, 10)  # connect, read

def _post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str] | None = None, max_attempts: int = 3):
    """
    Küçük bir retry ile JSON POST. 429/5xx'larda kısa bekleme ile tekrar dener.
    """
    if not url:
        log.info("Sheets sink disabled (no SHEETS_WEBHOOK_URL). Skip.")
        return {"ok": False, "skipped": True}

    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)

    body = json.dumps(payload, ensure_ascii=False)
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(url, headers=hdrs, data=body, timeout=DEFAULT_TIMEOUT)
            if resp.status_code < 400:
                # Apps Script genelde text döndürür; JSON parse etmeye çalışıp düşerse text’i döndür.
                try:
                    return {"ok": True, "status": resp.status_code, "json": resp.json()}
                except Exception:
                    return {"ok": True, "status": resp.status_code, "text": resp.text}
            # Geçici hata ise tekrar dene
            if resp.status_code in (429, 502, 503, 504) and attempt < max_attempts:
                time.sleep(1.0 * attempt)
                continue
            # Kalıcı hata
            log.error(f"Sheets sink failed status={resp.status_code} text={resp.text[:300]}")
            return {"ok": False, "status": resp.status_code, "text": resp.text[:300]}
        except requests.RequestException as e:
            if attempt < max_attempts:
                time.sleep(1.0 * attempt)
                continue
            log.exception(f"Sheets sink exception: {e}")
            return {"ok": False, "error": repr(e)}
    return {"ok": False, "error": "exhausted"}

def post_lead_to_sheets(user_id: str, brand: str, lead: Dict[str, Any], source: str = "instagram"):
    """
    Lead bittiğinde çağır. Apps Script Web App URL’ine JSON gönderir.
    Sheet tarafında şu alanları kaydetmek yaygın:
      user_id, brand, source, who, need, contact, ts
    """
    payload = {
        "user_id": user_id,
        "brand": brand,
        "source": source,
        "lead": {
            "who": lead.get("who", ""),
            "need": lead.get("need", ""),
            "contact": lead.get("contact", "")
        },
        "ts": int(time.time()),
        "secret": WEBHOOK_SECRET or ""
    }
    # İstersen secret'ı header’da da taşırız; Apps Script tarafında X-Webhook-Secret başlığını da okuyabilirsin.
    headers = {}
    if WEBHOOK_SECRET:
        headers["X-Webhook-Secret"] = WEBHOOK_SECRET

    return _post_json(SHEETS_WEBHOOK_URL, payload, headers=headers)