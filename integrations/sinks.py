# integrations/sinks.py
import os
import time 
import requests 
import logging
import json
import smtplib
import ssl
from email.mime.text import MIMEText

logger = logging.getLogger("igbot")

SHEETS_WEBHOOK_URL = os.getenv("SHEETS_WEBHOOK_URL", "").strip()
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "").strip()

def post_lead_to_sheets(user_id: str, brand: str, source: str, lead_dict: dict) -> dict:
    if not SHEETS_WEBHOOK_URL:
        logger.warning("sheets_skip: SHEETS_WEBHOOK_URL empty")
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
        logger.info(f"sheets_post status={resp.status_code} ok={body.get('ok')} dur_ms={dur}")
        return {"ok": bool(body.get("ok")), "status": resp.status_code, "dur_ms": dur, "body": body}
    except Exception:
        snip = (resp.text or "")[:140].replace("\n", " ")
        logger.info(f"sheets_post status={resp.status_code} body_snip={snip} dur_ms={dur}")
        return {"ok": False, "status": resp.status_code, "dur_ms": dur, "body_snip": snip}
    
# -----------------------------
#  ENV: Owner notification channels
#  (Hepsini .env'den oku ve hangisi lazımsa onu çağır.
# -----------------------------

OWNER_EMAIL_TO = os.getenv("OWNER_EMAIL_TO", "").strip()  # "a@b.com,b@c.com"
SMTP_HOST      = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER      = os.getenv("SMTP_USER", "").strip()
SMTP_PASS      = os.getenv("SMTP_PASS", "").strip()
SMTP_TLS       = os.getenv("SMTP_TLS", "1") == "1"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()  # string tutmak güvenli

OWNER_ALERT_WEBHOOK = os.getenv("OWNER_ALERT_WEBHOOK", "").strip()

# ========= Email Kanalı =========
def _send_email_owner(subject: str, body: str) -> dict:
    if not OWNER_EMAIL_TO or not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        return {"ok": False, "skipped": "email not configured"}

    recipients = [x.strip() for x in OWNER_EMAIL_TO.split(",") if x.strip()]
    if not recipients:
        return {"ok": False, "skipped": "no recipient"}

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(recipients)

    try:
        if SMTP_TLS:
            context = ssl.create_default_context()
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                server.starttls(context=context)
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_USER, recipients, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_USER, recipients, msg.as_string())

        logger.info("owner_email sent to=%s", recipients)
        return {"ok": True, "sent_to": recipients}
    except Exception as e:
        logger.warning("owner_email error: %r", e)
        return {"ok": False, "error": repr(e)}


# ========= Telegram Kanalı =========
def _send_telegram_owner(text: str) -> dict:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"ok": False, "skipped": "telegram not configured"}

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=8)
        ok = r.status_code == 200
        if ok:
            logger.info("owner_tg sent chat_id=%s", TELEGRAM_CHAT_ID)
            return {"ok": True, "status": r.status_code}
        else:
            logger.warning("owner_tg http_error status=%s body=%s", r.status_code, r.text[:500])
            return {"ok": False, "status": r.status_code, "body": r.text[:500]}
    except Exception as e:
        logger.warning("owner_tg error: %r", e)
        return {"ok": False, "error": repr(e)}


# ========= Webhook Kanalı =========
def _post_owner_webhook(payload: dict) -> dict:
    if not OWNER_ALERT_WEBHOOK:
        return {"ok": False, "skipped": "webhook not configured"}
    try:
        r = requests.post(OWNER_ALERT_WEBHOOK, json=payload, timeout=8)
        ok = 200 <= r.status_code < 300
        if ok:
            logger.info("owner_webhook posted")
            return {"ok": True, "status": r.status_code}
        else:
            logger.warning("owner_webhook http_error status=%s body=%s", r.status_code, r.text[:500])
            return {"ok": False, "status": r.status_code, "body": r.text[:500]}
    except Exception as e:
        logger.warning("owner_webhook error: %r", e)
        return {"ok": False, "error": repr(e)}
    
def notify_owner(event: str, summary_text: str, meta: dict | None = None) -> dict:
    """
    Engine 'insan devreye girsin' dediğinde burası çağrılır.
    Hangi kanallar .env'de doluysa oraya gönderir.
    Dönüş: her kanala ait sonuçların sözlüğü.
    """
    meta = meta or {}
    # E-posta gövdesi
    subject = f"[IG Bot] {event}"
    body = summary_text
    if meta:
        try:
            body += "\n\n" + json.dumps(meta, ensure_ascii=False, indent=2)
        except Exception:
            body += f"\n\n(meta: {meta})"

    # Telegram gövdesi (daha kompakt)
    tg_text = f"{event}\n\n{summary_text}"
    if meta:
        try:
            tg_text += "\n\n" + json.dumps(meta, ensure_ascii=False)
        except Exception:
            tg_text += f"\n\n(meta: {meta})"

    results = {
        "email":   _send_email_owner(subject, body),
        "telegram": _send_telegram_owner(tg_text),
        "webhook": _post_owner_webhook({
            "event": event,
            "summary": summary_text,
            "meta": meta,
        }),
    }
    logger.info("notify_owner results=%s", results)
    return results