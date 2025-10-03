# integrations/sinks.py
import os, time, requests, logging, json

log = logging.getLogger("igbot")

SHEETS_WEBHOOK_URL = os.getenv("SHEETS_WEBHOOK_URL", "").strip()
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "").strip()

def post_lead_to_sheets(user_id: str, brand: str, source: str, lead_dict: dict) -> dict:
    if not SHEETS_WEBHOOK_URL:
        log.warning("sheets_skip: SHEETS_WEBHOOK_URL empty")
        return {"ok": False, "skip": "no_url"}

    payload = {
        "user_id": user_id,
        "brand": brand,
        "source": source,
        "lead": lead_dict or {},
        "secret": WEBHOOK_SECRET,
    }

    t0 = time.time()
    resp = requests.post(SHEETS_WEBHOOK_URL, json=payload, timeout=10)
    dur = int((time.time() - t0) * 1000)

    try:
        body = resp.json()
        log.info(f"sheets_post status={resp.status_code} ok={body.get('ok')} dur_ms={dur}")
        return {"ok": bool(body.get("ok")), "status": resp.status_code, "dur_ms": dur, "body": body}
    except Exception:
        snip = (resp.text or "")[:140].replace("\n", " ")
        log.info(f"sheets_post status={resp.status_code} body_snip={snip} dur_ms={dur}")
        return {"ok": False, "status": resp.status_code, "dur_ms": dur, "body_snip": snip}