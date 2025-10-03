# engine.py
import time
from typing import Callable, Dict, Any, List
from integrations.sinks import post_lead_to_sheets


class RespondEngine:
    """
    LLM'siz v1 için:
    - YAML tabanlı SSS/Lead/Menu
    - Quick Replies (kullanıcı tıklasın; /komut yazmasın)
    - İş saatleri farkındalığı
    - Basit oturum (RAM içi)
    """
    def __init__(
        self,
        flow: Dict[str, Any],
        conf_brand: Callable[[], str],
        conf_cal_link: Callable[[], str],
        is_business_hours: Callable[[], bool],
        render_double_brace: Callable[[str, Dict[str, Any]], str],
    ):
        self.FLOW = flow
        self.conf_brand = conf_brand
        self.conf_cal_link = conf_cal_link
        self.is_business_hours = is_business_hours
        self.render_double_brace = render_double_brace

        # Oturum ve 24h pencere izleyicileri
        self.SESSIONS: Dict[str, Dict[str, Any]] = {}
        self.LAST_USER_TS: Dict[str, int] = {}
        self.SESSION_TTL = 60 * 60  # 1 saat

    def reload_flow(self, new_flow: Dict[str, Any]):
        """
        Hot-reload için: dışarıdan güncel FLOW sözlüğünü al ve referansı güncelle.
        """
        self.FLOW = new_flow or {}
    
    
        # ---------- Validators ----------
    def _is_valid_email(self, s: str) -> bool:
        """
        Basit e-posta kontrolü:
        - Boş değil
        - Bir tane '@' var (en azından bir '@' içeriyor)
        - '@' sonrası kısımda bir '.' var (örn: gmail.com)
        - Boşluk yok
        Bu regex kadar sert değil ama pratikte saçmalıkları eler.
        """
        if not s:
            return False
        s = s.strip()
        return ("@" in s) and ("." in s.split("@")[-1]) and (" " not in s)

    def _is_valid_phone(self, s: str) -> bool:
        """
        Telefonu 'rakam dışını at' yaklaşımıyla normalize ederiz:
        - Tüm karakterlerden sadece rakamları topla
        - 10–15 arası rakam varsa 'olur' say
        Neden 10–15? Türkiye’de 10 (baştaki 0’sız 10), uluslararası +90 dahil olunca 12–13,
        dünya genelinde toleranslı bir aralık.
        """
        if not s:
            return False
        digits = "".join(ch for ch in s if ch.isdigit())
        return 10 <= len(digits) <= 15

    def _looks_like_contact(self, s: str) -> bool:
        """
        'Bu bir iletişim bilgisine benziyor mu?' üst kuralı:
        - E-posta veya telefon kurallarından biri tutarsa True.
        Tek noktadan çağırıp karar veriyoruz (ileride kuralları geliştirirsek tek yerden güncelleriz).
        """
        return self._is_valid_email(s) or self._is_valid_phone(s)

    # ---------- Session Helpers ----------
    def _now(self) -> int:
        return int(time.time())

    def touch_user(self, uid: str):
        """24h pencere için son kullanıcı mesaj saatini güncelle."""
        self.LAST_USER_TS[uid] = self._now()

    def get_session(self, uid: str) -> Dict[str, Any]:
        s = self.SESSIONS.get(uid)
        if (not s) or (self._now() - s.get("ts", 0) > self.SESSION_TTL):
            s = {"state": "idle", "data": {}, "ts": self._now(), "lead_i": 0}
            self.SESSIONS[uid] = s
        return s

    def set_state(self, uid: str, state: str):
        s = self.get_session(uid)
        s["state"] = state
        s["ts"] = self._now()
        return s

    def set_kv(self, uid: str, key: str, val):
        s = self.get_session(uid)
        if "." in key:
            head, tail = key.split(".", 1)
            if head not in s["data"]:
                s["data"][head] = {}
            s["data"][head][tail] = val
        else:
            s["data"][key] = val
        s["ts"] = self._now()
        return s

    # ---------- UI Helpers (Quick Replies) ----------
    def _qr(self, title: str, payload: str) -> Dict[str, str]:
        # Instagram Quick Reply: content_type "text" olmalı; title (27 char sınırı civarı),
        # payload sunucuyu bilgilendirir (message.quick_reply.payload ile gelir).
        return {"content_type": "text", "title": title[:25], "payload": payload}

    def menu_qr(self) -> List[Dict[str, str]]:
        """Ana giriş seçenekleri (sektör-agnostik)."""
        return [
            self._qr("Paketler", "UI:PRICING"),
            self._qr("Randevu Al", "UI:DEMO"),
            self._qr("İhtiyacımı İlet", "UI:LEAD"),
            self._qr("Canlı Destek", "UI:HUMAN"),
        ]

    def lead_next_qr(self) -> List[Dict[str, str]]:
        """Lead akışı sırasında mini kısayollar (opsiyonel)."""
        return [
            self._qr("Devam", "UI:LEAD_CONT"),
            self._qr("Vazgeç", "UI:LEAD_CANCEL"),
        ]

    # ---------- Content Builders (YAML) ----------
    def menu_text(self) -> str:
        cfg = self.FLOW.get("menu") or {}
        header = (cfg.get("header") or "{brand} Menü:").format(
            brand=self.conf_brand(), cal_link=self.conf_cal_link()
        )
        items = "\n".join(cfg.get("items") or [])
        footer = (cfg.get("footer") or "").format(
            brand=self.conf_brand(), cal_link=self.conf_cal_link()
        )
        return "\n".join([header, items, footer]).strip()

    def try_faq_reply(self, text: str) -> str | None:
        faqs = self.FLOW.get("faq") or []
        t = (text or "").lower()
        for f in faqs:
            keys = [k.lower() for k in (f.get("keys") or [])]
            if any(k in t for k in keys):
                base = (f.get("reply") or "").format(
                    brand=self.conf_brand(), cal_link=self.conf_cal_link()
                )
                cta = f.get("cta")
                return base + (f"\n\nDevam için dokunun: Paketler" if cta == "/lead" else "")
        return None

    # ---------- Lead Flow ----------
    def start_lead(self, uid: str) -> Dict[str, Any]:
        s = self.get_session(uid)
        s["lead_i"] = 0
        s["lead_data"] = {}
        self.set_state(uid, "lead.active")

        steps = ((self.FLOW.get("lead") or {}).get("steps") or [])
        if not steps:
            self.set_state(uid, "idle")
            return {"text": "Lead formu şu an kapalı görünüyor.", "quick_replies": self.menu_qr()}

        return {"text": steps[0].get("ask") or "Başlayalım: adınızı alabilir miyim?", "quick_replies": self.lead_next_qr()}

    def progress_lead(self, uid: str, incoming_text: str) -> Dict[str, Any]:
        s = self.get_session(uid)
        steps = ((self.FLOW.get("lead") or {}).get("steps") or [])
        i = int(s.get("lead_i", 0))

        # 1) Bu adımdaki cevabı kaydet
        if 0 <= i < len(steps):
            save_as = steps[i].get("save_as")
            if save_as:
                self.set_kv(uid, f"lead_data.{save_as}", (incoming_text or "").strip())
                s = self.get_session(uid)
            
            # >>> tam burası: "contact" adımıysa ve değer geçersizse AYNI ADIMDA KAL <<<
            if save_as == "contact":
                user_val = (incoming_text or "").strip()
                if not self._looks_like_contact(user_val):
                    retry_msg = (
                        "Bunu bir iletişim bilgisi olarak anlayamadım. "
                        "Lütfen e-posta (örn: ad@site.com) ya da telefon (örn: +90 5xx xxx xx xx) yazın."
                    )
                    return {
                        "text": retry_msg,
                        "quick_replies": self.lead_next_qr()
                    }

        # 2) Sonraki adıma geç
        i += 1
        s["lead_i"] = i

        # 3) Hâlâ soru varsa sor ve küçük QR’ları göster
        if i < len(steps):
            return {
                "text": steps[i].get("ask") or "Devam edelim…",
                "quick_replies": self.lead_next_qr()
            }

        # 4) Akış bitti → özet+after hazırla
        lead_cfg = (self.FLOW.get("lead") or {})
        data = s.get("data") or {}
        lead_data = data.get("lead_data") or {}

        off_note = "Şu an iş saatleri dışında olabiliriz. " if not self.is_business_hours() else ""

        header = (lead_cfg.get("summary_header") or "Özet:")
        summary_tpl = (lead_cfg.get("summary") or "").strip()
        summary = self.render_double_brace(summary_tpl, lead_data)

        after = (lead_cfg.get("after") or "").format(
            brand=self.conf_brand(),
            cal_link=self.conf_cal_link()
        )
        after = after.replace("{{offhours_note}}", off_note).replace("{offhours_note}", off_note)

        # 5) State’i kapat
        self.set_state(uid, "idle")

        # 6) Sheets’e POST at  ✅ (imzayı anahtar argümanlarla ver)
        try:
            post_lead_to_sheets(
                user_id=uid,
                brand=self.conf_brand(),
                source="instagram",
                lead_dict={
                    "who": lead_data.get("who", ""),
                    "need": lead_data.get("need", ""),
                    "contact": lead_data.get("contact", "")
                }
            )
        except Exception as e:
            import logging
            logging.getLogger("igbot").warning(f"Sheets sink post failed: {e}")


        # 7) Kullanıcıya final mesajı + ana menü QR’ları
        return {
            "text": f"{header}\n{summary}\n\n{after}".strip(),
            "quick_replies": self.menu_qr()
        }

    # ---------- Main Router ----------
    def respond(self, uid: str, text: str, quick_payload: str | None = None) -> Dict[str, Any]:
        """
        Dönüş formatı:
        { "text": "...", "quick_replies": [ ... ] }    # quick_replies opsiyonel
        """
        s = self.get_session(uid)

        # 0) Quick Reply seçimleri (payload) önce işlenir
        if quick_payload:
            qp = quick_payload.upper()
            if qp == "UI:PRICING":
                ans = self.try_faq_reply("fiyat")
                return {"text": ans or "Paket bilgileri için birazdan güncelleyeceğiz.", "quick_replies": self.menu_qr()}
            if qp == "UI:DEMO":
                txt = f"Takvimden randevu alabilirsiniz: {self.conf_cal_link()}"
                if not self.is_business_hours():
                    txt += "\nNot: Şu an iş saatleri dışında olabiliriz."
                return {"text": txt, "quick_replies": self.menu_qr()}
            if qp == "UI:LEAD":
                return self.start_lead(uid)
            if qp == "UI:HUMAN":
                # v1: bilgilendir; v1.2’de handover ekleyeceğiz
                return {"text": "Canlı destek talebinizi ilettik. Bu arada ihtiyaçlarınızı kısaca /lead ile paylaşabilirsiniz.", "quick_replies": self.menu_qr()}
            if qp == "UI:LEAD_CONT":
                # lead içinde zaten progress çağrısı yapılacak; burada menüye dönmeyelim
                pass
            if qp == "UI:LEAD_CANCEL":
                self.set_state(uid, "idle")
                return {"text": "Tamamdır, lead formunu iptal ettik. Nasıl yardımcı olabilirim?", "quick_replies": self.menu_qr()}

        # 1) Lead akışı aktifse → ilerlet
        if s["state"] == "lead.active":
            return self.progress_lead(uid, text)

        t = (text or "").strip().lower()

        # 2) Selamlama/İlk temas: doğal bir giriş cümlesi + butonlar
        if any(k in t for k in ["merhaba", "selam", "selamlar", "slm", "hi", "hello", "hey"]):
            if not self.is_business_hours():
                fb = (self.FLOW.get("fallback") or {}).get("off_hours") or \
                    "Şu an iş saatleri dışında olabiliriz. İsterseniz 'İhtiyacımı İlet' ile bilgilerinizi bırakın, ilk iş dönüş yapalım."
                return {"text": fb, "quick_replies": self.menu_qr()}
            # iş saatleri içindeyse standart selamlama
            greet = (
                "Merhaba! 👋 Size en hızlı nasıl yardımcı olabilirim?\n"
                "Aşağıdaki seçeneklerden birini dokunarak seçebilirsiniz."
            )
            return {"text": greet, "quick_replies": self.menu_qr()}


        # 3) SSS anahtar eşleşmesi
        faq = self.try_faq_reply(text)
        if faq:
            return {"text": faq, "quick_replies": self.menu_qr()}

        # 4) İş saati dışı fallback
        if not self.is_business_hours():
            fb = (self.FLOW.get("fallback") or {}).get("off_hours") or \
                 "Şu an iş saatleri dışında olabiliriz. İsterseniz 'İhtiyacımı İlet' ile bilgilerinizi bırakın, ilk iş dönüş yapalım."
            return {"text": fb, "quick_replies": self.menu_qr()}

        # 5) Genel fallback (iş saati içi)
        fb = (self.FLOW.get("fallback") or {}).get("in_hours") or \
             "Anladım. Aşağıdaki seçeneklerden seçim yapabilirsiniz."
        return {"text": fb, "quick_replies": self.menu_qr()}