import hmac, hashlib, os, json, requests, logging, time, random, uuid, asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from dotenv import load_dotenv
from collections import OrderedDict
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
# --- [PATCH A: YAML & template helpers] ---
import yaml
from pathlib import Path
from string import Template

FLOWS_PATH = Path(__file__).parent / "flows" / "default.yaml"
FLOW = {}

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

logging.getLogger("urllib3").setLevel(logging.WARNING)

# --- [PATCH B: YAML'dan config yükleme + saat yardımcıları] ---
from zoneinfo import ZoneInfo
TIMEZONE = ZoneInfo("Europe/Istanbul")  # istersen .env ile parametrize ederiz

def load_flow():
    """flows/default.yaml dosyasını belleğe yükle."""
    global FLOW
    try:
        with open(FLOWS_PATH, "r", encoding="utf-8") as f:
            FLOW = yaml.safe_load(f) or {}
        logger.info(f"Flow loaded from {FLOWS_PATH}")
    except Exception as e:
        logger.error(f"Flow load error: {e}")
        FLOW = {}

def conf_brand():
    return (FLOW.get("brand") or "YourBot")

def conf_cal_link():
    return (FLOW.get("cal_link") or "https://cal.com/yourname/15min")

def conf_hours():
    """YAML'daki business_hours'tan start/end saatlerini al."""
    bh = FLOW.get("business_hours") or {}
    start = int(bh.get("start", 9))
    end   = int(bh.get("end", 18))
    return start, end

def now_in_tz():
    return datetime.now(TIMEZONE)

def is_business_hours():
    start, end = conf_hours()
    h = now_in_tz().hour
    return start <= h < end

def render_double_brace(text: str, data: dict) -> str:
    """'{{key}}' yer tutucularını data[key] ile değiştirir."""
    out = text or ""
    for k, v in (data or {}).items():
        out = out.replace(f"{{{{{k}}}}}", str(v))
    return out

# Uygulama ayağa kalkarken YAML'ı bir kez yükle
load_flow()

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
    """HTTP Retry-After varsa saniye cinsinden döndür (sayı ya da HTTP-date destekli)."""
    ra = resp.headers.get("Retry-After")
    if not ra:
        return 0
    ra = str(ra).strip()
    if ra.isdigit():
        return int(ra)
    try:
        dt = parsedate_to_datetime(ra)
        now = datetime.now(dt.tzinfo or timezone.utc)
        return max(0, int((dt - now).total_seconds()))
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

def _graph_smoke_check() -> dict:
    """
    Health amaçlı hafif kontrol:
    - Page ID'yi token ile çekebiliyor muyuz?
    - Ağ/HTTP/Graph hatalarını döndür.
    """
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PAGE_ID}"
    params = {"access_token": PAGE_ACCESS_TOKEN, "fields": "id"}
    try:
        t0 = time.time()
        resp = SESSION.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        dur_ms = int((time.time() - t0) * 1000)
        try:
            data = resp.json()
        except Exception:
            data = {"_nonjson": resp.text}

        return {
            "ok": resp.status_code == 200,
            "status": resp.status_code,
            "dur_ms": dur_ms,
            "body": data,
        }
    except requests.Timeout:
        return {"ok": False, "status": 599, "error": "timeout"}
    except Exception as e:
        return {"ok": False, "status": 598, "error": repr(e)}


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

@app.get("/healthz")
def healthz(check: str | None = None):
    """
    Basit sağlık ucu:
    - /healthz             -> {"status":"ok"}
    - /healthz?check=graph -> Page token ve Graph çıkışı duman testi
    """
    if check == "graph":
        result = _graph_smoke_check()
        code = 200 if result.get("ok") else 503
        return JSONResponse(
            content={"status": "ok" if result.get("ok") else "fail", "graph": result},
            status_code=code
        )
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

# --- [PATCH S1: Basit oturum / session store] ---
from time import time as _now

SESSIONS = {}           # { user_id: {"state": "idle"|"lead.active", "data": {...}, "ts": epoch, "lead_i": idx} }
SESSION_TTL = 60 * 60   # 1 saat

def get_session(uid: str):
    s = SESSIONS.get(uid)
    if not s or (_now() - s.get("ts", 0) > SESSION_TTL):
        s = {"state": "idle", "data": {}, "ts": _now(), "lead_i": 0}
        SESSIONS[uid] = s
    return s

def set_state(uid: str, state: str):
    s = get_session(uid)
    s["state"] = state
    s["ts"] = _now()
    return s

def set_kv(uid: str, key: str, val):
    s = get_session(uid)
    # "lead_data.x" gibi noktalı anahtarları destekleyelim
    if "." in key:
        head, tail = key.split(".", 1)
        if head not in s["data"]:
            s["data"][head] = {}
        s["data"][head][tail] = val
    else:
        s["data"][key] = val
    s["ts"] = _now()
    return s

# --- [PATCH C: Menü + SSS + Lead (YAML kaynaklı Router)] ---

# Kullanıcının son mesaj saatini izleriz (24h pencere için ileride kullanacağız)
LAST_USER_TS = {}  # { user_id: epoch_seconds }

def menu_text():
    """
    YAML'daki menu.header / menu.items / menu.footer'ı birleştirir.
    {brand} ve {cal_link} tek-süslü yer tutucularını str.format ile doldurur.
    """
    cfg = FLOW.get("menu") or {}
    header = (cfg.get("header") or "{brand} Menü:").format(
        brand=conf_brand(), cal_link=conf_cal_link()
    )
    items = "\n".join(cfg.get("items") or [])
    footer = (cfg.get("footer") or "").format(
        brand=conf_brand(), cal_link=conf_cal_link()
    )
    return "\n".join([header, items, footer]).strip()

def try_faq_reply(text: str):
    """
    YAML'daki faq listesinde keys alanlarına göre anahtar eşleşmesi yapar.
    Eşleşirse reply'i döner; {brand}/{cal_link} tek-süslü alanlarını doldurur.
    CTA varsa en sona 'Devam: <cta>' ekler.
    """
    faqs = FLOW.get("faq") or []
    t = (text or "").lower()
    for f in faqs:
        keys = [k.lower() for k in (f.get("keys") or [])]
        if any(k in t for k in keys):
            base = (f.get("reply") or "").format(
                brand=conf_brand(), cal_link=conf_cal_link()
            )
            cta = f.get("cta")
            return base + (f"\n\nDevam: {cta}" if cta else "")
    return None

def start_lead(uid: str):
    """
    Lead akışını başlatır: lead_i = 0 ayarlanır, first question sorulur.
    adımlar YAML: lead.steps altında; ask/save_as alanları vardır.
    """
    s = get_session(uid)
    s["lead_i"] = 0
    s["lead_data"] = {}
    set_state(uid, "lead.active")

    steps = ((FLOW.get("lead") or {}).get("steps") or [])
    if not steps:
        set_state(uid, "idle")
        return "Lead formu şu an kapalı görünüyor."

    # İlk soruyu sor
    return steps[0].get("ask") or "Başlayalım: adınızı alabilir miyim?"

def progress_lead(uid: str, incoming_text: str):
    """
    Lead akışını ilerletir:
    - O anki adımın save_as'ı varsa gelen metni kaydeder
    - Bir sonraki adıma geçer; bittiğinde özet + after metnini oluşturur
    - Özet içinde {{who}} gibi çift-süslü alanlar lead_data ile doldurulur
    - after içinde {brand} / {cal_link} tek-süslü alanlar str.format ile doldurulur
    """
    s = get_session(uid)
    steps = ((FLOW.get("lead") or {}).get("steps") or [])
    i = int(s.get("lead_i", 0))

    # Geçerli adımı kaydet
    if 0 <= i < len(steps):
        save_as = steps[i].get("save_as")
        if save_as:
            set_kv(uid, f"lead_data.{save_as}", (incoming_text or "").strip())
            s = get_session(uid)  # referansı yenile

    # Bir sonraki adıma geç
    i += 1
    s["lead_i"] = i

    # Sıradaki soru varsa onu sor
    if i < len(steps):
        return steps[i].get("ask") or "Devam edelim…"

    # Akış bitti → özetle ve state'i idle'a döndür
    lead_cfg = (FLOW.get("lead") or {})
    data = s.get("data") or {}
    lead_data = data.get("lead_data") or {}

    # İş saati durumuna göre kısa not
    off_note = "Şu an iş saatleri dışında olabiliriz. " if not is_business_hours() else ""

    header = (lead_cfg.get("summary_header") or "Özet:")
    summary_tpl = (lead_cfg.get("summary") or "").strip()
    # Çift-süslü {{who}} gibi alanları lead_data ile doldur
    summary = render_double_brace(summary_tpl, lead_data)

    # after metnindeki {brand}/{cal_link} alanlarını doldur
    after = (lead_cfg.get("after") or "").format(
        brand=conf_brand(),
        cal_link=conf_cal_link()
    )
    # after içinde {{offhours_note}} varsa onu da yerleştir
    after = after.replace("{{offhours_note}}", off_note)

    set_state(uid, "idle")
    return f"{header}\n{summary}\n\n{after}".strip()

def respond(uid: str, text: str) -> str:
    """
    Router'ın kalbi:
    1) Eğer kullanıcı lead akışındaysa -> progress_lead
    2) Komutlar: /menu, /lead, /demo, /fiyat, /help, /human
    3) Serbest metinde önce FAQ eşleşmesi
    4) İş saati dışı fallback
    5) Genel fallback (iş saati içi)
    """
    s = get_session(uid)

    # 1) Lead akışı aktifse önce onu ilerlet
    if s["state"] == "lead.active":
        return progress_lead(uid, text)

    t = (text or "").strip().lower()

    # 2) Komutlar
    if t in ("/start", "/menu"):
        return menu_text()

    if t in ("/lead",):
        return start_lead(uid)

    if t in ("/demo", "randevu", "call", "meeting"):
        return (
            f"Kısa bir demo ayarlayalım: {conf_cal_link()}\n"
            f"{'Not: Şu an iş saatleri dışında olabiliriz.' if not is_business_hours() else ''}"
        )

    if t in ("/fiyat", "/price", "fiyat", "ücret", "paket", "price"):
        ans = try_faq_reply("fiyat")
        return ans or "Paket bilgileri için birazdan güncelleyeceğiz."

    if t in ("/help", "yardım"):
        return menu_text()

    if t in ("/human", "insan", "canlı", "müşteri temsilcisi"):
        # v1: sadece bilgilendir. Sprint-2’de gerçek handover çağrısı eklenecek.
        return (
            "Talebinizi canlı destek ekibine iletiyoruz. "
            "Bu arada /lead ile temel bilgileri paylaşırsanız daha hızlı dönüş yapabiliriz."
        )

    # 3) Serbest metinde FAQ dene
    faq = try_faq_reply(text)
    if faq:
        return faq

    # 4) İş saati dışı fallback
    if not is_business_hours():
        fb = (FLOW.get("fallback") or {}).get("off_hours") or \
             "Şu an iş saatleri dışında olabiliriz. /lead bırakabilirsiniz."
        return fb

    # 5) Genel fallback (iş saati içi)
    fb = (FLOW.get("fallback") or {}).get("in_hours") or \
         "Anladım. /fiyat, /demo veya /lead ile ilerleyebiliriz."
    return fb


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

def iter_text_events(entry: dict):
    """
    entry içinden (messaging ve changes/messages) metin DM'leri uniform biçimde üretir.
    yield: (sender_id, text, mid, is_echo)
    """
    # Kaynak 1: messaging
    for event in entry.get("messaging", []):
        sender_id = (event.get("sender") or {}).get("id")
        msg = event.get("message") or {}
        text = msg.get("text")
        mid  = msg.get("mid")
        is_echo = bool(msg.get("is_echo"))
        yield sender_id, text, mid, is_echo

    # Kaynak 2: changes
    for change in entry.get("changes", []):
        if change.get("field") != "messages":
            continue
        val = change.get("value") or {}
        sender_id = (val.get("sender") or {}).get("id")
        msg = val.get("message") or {}
        text = msg.get("text")
        mid  = msg.get("mid")
        is_echo = bool(msg.get("is_echo"))
        yield sender_id, text, mid, is_echo


async def handle_text_message(sender_id: str, text: str, mid: str, is_echo: bool,
                        business_ig_id: str, req_id: str | None = None):
    """
    Tek bir metin mesajını işler: guard'lar -> typing -> reply -> send.
    messaging/changes her ikisi de bunu çağırır.
    """
    # --- Guard'lar ---
    if is_echo:
        return
    if sender_id == business_ig_id or sender_id == PAGE_ID:
        return
    if not sender_id or not text:
        return
    if _seen_mid(mid):
        return

    # 24h pencere izleme: kullanıcıdan mesaj gelir gelmez işaretle
    LAST_USER_TS[sender_id] = int(time.time())

    # UX: kısa "yazıyor..." balonu
    await typing_indicator(sender_id, req_id=req_id, duration=1.0)

    # Temel cevap (istersen router'a geçeriz)
    reply = respond(sender_id, text)
    send_text_message(sender_id, reply, req_id=req_id)


def send_text_message(ig_sid: str, text: str, req_id: str | None = None):
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PAGE_ID}/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": ig_sid}, "message": {"text": text}}
    # İstersen deneme sayısını parametrele: max_attempts=5
    return _graph_send_with_retry(url, params, payload, max_attempts=5,req_id=req_id)

def send_sender_action(ig_sid: str, action: str, req_id: str | None = None):
    """
    IG kullanıcısına sender_action (mark_seen / typing_on) gönder.
    """
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PAGE_ID}/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {"recipient": {"id": ig_sid}, "sender_action": action}
    return _graph_send_with_retry(url, params, payload, max_attempts=5, req_id=req_id)

async def typing_indicator(ig_sid: str, req_id: str | None = None, duration: float = 1.0):
    """
    IG'de typing_on destekli. Balonun görünmesi için kısa bir süre açık tutuyoruz.
    """
    send_sender_action(ig_sid, "typing_on", req_id=req_id)
    await asyncio.sleep(duration)
    # istersen typing_off da atabilirsin; çoğu istemci otomatik söndürür:
    # send_sender_action(ig_sid, "typing_off", req_id=req_id)

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

            for sender_id, text, mid, is_echo in iter_text_events(entry):
                try:
                    await handle_text_message(sender_id, text, mid, is_echo, business_ig_id, req_id=req_id)
                except Exception as e:
                    logger.exception(f"send_exc {e}")

        return {"status": "ok"}
    except Exception as e:
        logger.exception(f"parse_exc {e} payload={json.dumps(data)[:1000]}")
        return {"status": "ok"}