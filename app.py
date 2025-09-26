import hmac, hashlib, os, json, requests, logging, time, random, uuid
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv
from collections import OrderedDict
from email.utils import parsedate_to_datetime


load_dotenv()
PAGE_ID = os.getenv("PAGE_ID")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
APP_SECRET   = os.getenv("APP_SECRET")          

GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v21.0")
DEBUG = os.getenv("DEBUG", "0") == "1"   # <-- EKLEDİK

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("igbot")

# İsteğe bağlı koruma/log (önerilir)
if not PAGE_ID or not PAGE_ACCESS_TOKEN:
    logger.warning("PAGE_ID veya PAGE_ACCESS_TOKEN boş. Send API denemeleri başarısız olabilir.")


# Güvenlik: ortam değişkeni yoksa erken patlat:
if not VERIFY_TOKEN:
    raise RuntimeError("VERIFY_TOKEN not set in .env")
if not APP_SECRET:
    raise RuntimeError("APP_SECRET not set in .env")

# --- HTTP bağlantı havuzu + zaman aşımı (retry/backoff için temel) ---
DEFAULT_TIMEOUT = (5, 10)  # (connect_timeout, read_timeout) saniye
SESSION = requests.Session()

def _parse_retry_after(resp) -> int:
    """HTTP Retry-After varsa saniye olarak döndür; yoksa 0."""
    ra = resp.headers.get("Retry-After")
    try:
        return int(ra)
    except Exception:
        return 0

def _is_transient_http(status: int) -> bool:
    """Geçici kabul ettiğimiz HTTP durumları."""
    return status in (429, 502, 503, 504)

def _is_transient_graph(data: dict) -> bool:
    """Graph API error gövdesinden geçici hata sinyali."""
    try:
        err = data.get("error", {})
        code = err.get("code")
        # 1,2,4: internal/transient; 613: rate limit
        return code in (1, 2, 4, 613)
    except Exception:
        return False

def _sleep_backoff(attempt: int, retry_after: int):
    """Retry öncesi bekleme: Retry-After varsa onu, yoksa exponential + jitter."""
    if retry_after and retry_after > 0:
        time.sleep(retry_after)
        return
    base = min(2 ** attempt, 32)      # 1,2,4,8,16,32 (cap 32s)
    jitter = random.uniform(0, 0.5)   # küçük jitter
    time.sleep(base + jitter)

def _graph_send_with_retry(url: str, params: dict, payload: dict, max_attempts: int = 5, req_id: str | None = None):
    for attempt in range(1, max_attempts + 1):
        t0 = time.time()
        resp = SESSION.post(url, params=params, json=payload, timeout=DEFAULT_TIMEOUT)
        dur_ms = int((time.time() - t0) * 1000)

        # JSON parse etmeye çalış
        try:
            data = resp.json()
        except Exception:
            data = {"_nonjson": resp.text}

        # Başarılıysa bitir
        if resp.status_code < 400:
            logger.info(f"req_id={req_id} send_ok status={resp.status_code} dur_ms={dur_ms} mid={data.get('message_id')}")
            return resp.status_code, data

        # Geçici hata mı?
        retry_after = _parse_retry_after(resp)
        transient = _is_transient_http(resp.status_code) or _is_transient_graph(data)

        if transient and attempt < max_attempts:
            logger.error(f"req_id={req_id} send_err_transient status={resp.status_code} attempt={attempt} dur_ms={dur_ms} body={data}")
            _sleep_backoff(attempt, retry_after)
            continue  # yeniden dene

        # Geçici değilse (veya denemeler bitti ise): vazgeç
        logger.error(f"req_id={req_id} send_err_non_retriable status={resp.status_code} dur_ms={dur_ms} body={data}")
        return resp.status_code, data

    # Teorik olarak buraya gelmez
    return 599, {"error": "exhausted retries"}

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok"}

def verify_xhub_signature(app_secret: str, signature_header_256: str, body: bytes, signature_header_1: str = None) -> bool:
    """
    Meta imza doğrulaması. Öncelik sha256; yoksa sha1'e bakar.
    """
    if signature_header_256 and signature_header_256.lower().startswith("sha256="):
        sent = signature_header_256.split("=", 1)[1].strip()
        mac = hmac.new(app_secret.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
        return hmac.compare_digest(sent, mac.hexdigest())
    
    if signature_header_1 and signature_header_1.lower().startswith("sha1="):
        sent = signature_header_1.split("=", 1)[1].strip()
        mac = hmac.new(app_secret.encode("utf-8"), msg=body, digestmod=hashlib.sha1)
        return hmac.compare_digest(sent, mac.hexdigest())
    
    return False

# --- Dedup için basit LRU benzeri bir sözlük ---
RECENT_MIDS = OrderedDict()

def _seen_mid(mid: str, cap: int = 1000) -> bool:
    """Aynı mesajı iki kez işlemeyi engelle (hem messaging hem changes döngüsünde yakalarsan)."""
    if not mid:
        return False
    if mid in RECENT_MIDS:
        return True
    RECENT_MIDS[mid] = None
    if len(RECENT_MIDS) > cap:
        RECENT_MIDS.popitem(last=False)
    return False


def basic_bot_reply(user_text: str) -> str:
    t = (user_text or "").strip().lower()
    if any(k in t for k in ["merhaba", "selam", "hi", "hello"]):
        return "Selam! Nasıl yardımcı olabilirim? 😊"
    if "fiyat" in t or "ücret" in t or "price" in t:
        return "Fiyatlarımız paketlere göre değişiyor. Starter paket aylık 49$. Detay ister misiniz?"
    if "demo" in t or "deneme" in t:
        return "Kısa bir demo planlayalım: https://cal.com/senin-adin/15min"
    return "Anladım, biraz daha açabilir misiniz?"

# --- R2 helpers: retry/backoff ---
def _parse_retry_after(hval: str) -> float:
    if not hval: return 0.0
    hval = str(hval).strip()
    if hval.isdigit(): return float(hval)
    try:
        dt = parsedate_to_datetime(hval)
        return max(0.0, (dt - dt.now(dt.tzinfo)).total_seconds())
    except Exception:
        return 0.0

def _compute_backoff(attempt: int, base: float = 0.5, cap: float = 20.0) -> float:
    expo = min(cap, base * (2 ** attempt))
    jitter = random.uniform(0, base)
    return expo + jitter
# --- /R2 helpers ---

def send_text_message(ig_sid: str, text: str, req_id: str | None = None):
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PAGE_ID}/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": ig_sid}, "message": {"text": text}}
    # İstersen deneme sayısını parametrele: max_attempts=5
    return _graph_send_with_retry(url, params, payload, max_attempts=5,req_id=req_id)

@app.get("/webhook", response_class=PlainTextResponse)
async def verify(req: Request):
    """
    Meta doğrulama isteği:
    ?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...
    Doğru token ise challenge'ı düz metin döneriz.
    """
    qp = dict(req.query_params)
    mode = qp.get("hub.mode")
    token = qp.get("hub.verify_token")
    challenge = qp.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(challenge or "", status_code=200)
    raise HTTPException(status_code=403, detail="Verification token mismatch.")


@app.post("/webhook")
async def webhook(req: Request):
    req_id = uuid.uuid4().hex[:12]
    # 1) İmza doğrulama için ham body + header'ı al
    raw = await req.body()
    sig256 = req.headers.get("X-Hub-Signature-256")
    sig1   = req.headers.get("X-Hub-Signature")

    # İmzaları görmek için debug (sadece DEBUG=1 iken)
    if DEBUG:
        expected = 'sha256=' + hmac.new(APP_SECRET.encode('utf-8'), raw, hashlib.sha256).hexdigest()
        logger.debug(f"req_id={req_id} sig_expected={expected} sent={(sig256 or sig1 or '')}")

    # 2) İmza yanlışsa 403
    if not verify_xhub_signature(APP_SECRET, sig256, raw, sig1):
        logger.warning(f"req_id={req_id} sig_mismatch")
        raise HTTPException(status_code=403, detail="Invalid signature")

    # 3) JSON parse et
    try:
        data = await req.json()
        # istersen debug log:
        logger.info(f"req_id={req_id} in_event {json.dumps(data)[:1000]}")
    except Exception:
        return {"status": "ok"}

    # 4) IG payload parse + cevap gönder
    try:
        if data.get("object") not in ("instagram", "page"):
            return {"status": "ignored"}

        for entry in data.get("entry", []):
            business_ig_id = str(entry.get("id") or "")  # işletme IG SID (self-sender kontrolü için)

            for event in entry.get("messaging", []):
                sender_id = (event.get("sender") or {}).get("id")
                msg = event.get("message") or {}
                text = msg.get("text")
                mid  = msg.get("mid")
                is_echo = bool(msg.get("is_echo"))

                # --- Guard'lar: neden? ---
                # 1) echo ise: bu mesajı biz göndermişiz, platform bize "bilgi" amaçlı yansıtır → cevaplama
                if is_echo:
                    continue
                # 2) kendimizsek: sender işletme IG (entry.id) veya Page ID ise kullanıcı değil → cevaplama
                if sender_id == business_ig_id or sender_id == PAGE_ID:
                    continue
                # 3) metin değilse (read/delivery vb.): cevaplama
                if not sender_id or not text:
                    continue
                # 4) aynı mid geldiyse (messaging + changes / retry): ikinci kez işlemeyi önle
                if _seen_mid(mid):
                    continue

                reply = basic_bot_reply(text)
                try:
                    send_text_message(sender_id, reply, req_id=req_id)
                except Exception as e:
                    logger.exception(f"send_exc {e}")


            for change in entry.get("changes", []):

                # Sadece DM mesaj alanını dinle
                if change.get("field") != "messages":
                    continue

                val = change.get("value") or {}
                sender_id = (val.get("sender") or {}).get("id")
                msg = val.get("message") or {}
                text = msg.get("text")
                mid = msg.get("mid")
                is_echo = bool(msg.get("is_echo"))

                # --- Guard'lar ---
                # 1) echo ise: bizim gönderdiğimiz mesajın geri bildirimi → cevaplama
                if is_echo:
                    continue
                # 2) kendimizsek: sender işletme IG (entry.id) veya Page ID ise kullanıcı değil → cevaplama
                if sender_id == business_ig_id or sender_id == PAGE_ID:
                    continue
                # 3) metin yoksa (read/delivery vb.) → cevaplama
                if not sender_id or not text:
                    continue
                # 4) aynı mid geldiyse (messaging + changes / retry) → ikinci kez işlemeyi önle
                if _seen_mid(mid):
                    continue

                reply = basic_bot_reply(text)
                try:
                    send_text_message(sender_id, reply, req_id=req_id)
                except Exception as e:
                    logger.exception(f"send_exc {e}")

        return {"status": "ok"}
    except Exception as e:
        logger.exception(f"parse_exc {e} payload={json.dumps(data)[:1000]}")
        return {"status": "ok"}