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
    
    def _qr_from_yaml(self) -> List[Dict[str, str]]:
        """YAML: ui.quick_replies içindeki başlık+payload’ları butona çevir."""
        ui = self.FLOW.get("ui") or {}
        qrs = []
        for item in (ui.get("quick_replies") or []):
            title = (item.get("title") or "")[:25]
            payload = item.get("payload") or ""
            if title and payload:
                qrs.append(self._qr(title, payload))
        return qrs[:13]

    def _qr_from_titles(self, titles: List[str]) -> List[Dict[str, str]]:
        """
        YAML’da tanımlı quick reply’ler içinden, verilen başlıklara karşılık gelen butonları getir.
        Başlık eşleşmesi case-insensitive yapılır.
        """
        if not titles:
            return []
        title_set = {t.strip().lower() for t in titles if t and isinstance(t, str)}
        ui = self.FLOW.get("ui") or {}
        out = []
        for item in (ui.get("quick_replies") or []):
            t = (item.get("title") or "").strip()
            if t.lower() in title_set:
                out.append(self._qr(t, item.get("payload") or ""))
        return out[:13]
    
    def _payload_to_action(self, payload: str) -> str | None:
        """
        YAML: ui.quick_replies[*].payload → action çöz.
        Örn: "UI:PRICING" -> "pricing"
        """
        if not payload:
            return None
        ui = self.FLOW.get("ui") or {}
        for item in (ui.get("quick_replies") or []):
            if (item.get("payload") or "").strip().upper() == payload.strip().upper():
                return (item.get("action") or "").strip().lower() or None
        return None

    def _greeting_text(self) -> str:
        """
        YAML: ui.greetings içinden saat durumuna göre metni getir.
        """
        ui = self.FLOW.get("ui") or {}
        g = ui.get("greetings") or {}
        raw = (g.get("in_hours") if self.is_business_hours() else g.get("off_hours")) \
              or ("Merhaba! 👋 Size nasıl yardımcı olabilirim?")
        # 1) {brand}/{cal_link}
        txt = raw.format(brand=self.conf_brand(), cal_link=self.conf_cal_link())
        # 2) {{offhours_note}} vs. kullanmak istersen
        txt = self.render_double_brace(txt, {"offhours_note": self._offhours_note()})
        return txt

    def _faq_hint(self) -> str:
        """
        YAML: ui.faq_hint varsa getir; yoksa varsayılan ipucu döndür.
        """
        ui = self.FLOW.get("ui") or {}
        return (ui.get("faq_hint") or 
                "Sorunuzu yazabilirsiniz; örnek: 'adres', 'çalışma saatleri', 'iade', 'teslimat'…")
    
    def _pretty_title(self, s: str) -> str:
        # Basit Türkçe başlık stilizasyonu (çok ileriye gitmeden)
        s = (s or "").strip()
        if not s:
            return s
        # 'kvkk' gibi bilinen kısaltmaları büyük yaz
        if s.upper() in {"KVKK", "SSS"}:
            return s.upper()
        # Diğerlerini ilk harf büyük
        return s[:1].upper() + s[1:]
    
    def _faq_topic_qr(self, limit: int = 8) -> List[Dict[str, str]]:
        """
        FLOW.faq içinden her öğenin ilk 'keys' elemanını alır,
        başlıklaştırır ve payload=FAQ:<keyword> olacak şekilde quick reply listesi döndürür.
        """
        faqs = self.FLOW.get("faq") or []
        out = []
        seen_titles = set()
        for f in faqs[:limit]:
            keys = f.get("keys") or []
            if not keys:
                continue
            key = str(keys[0]).strip()
            if not key:
                continue
            title = self._pretty_title(key)
            if title.lower() in seen_titles:
                continue
            seen_titles.add(title.lower())
            payload = f"FAQ:{key}"
            out.append(self._qr(title, payload))
            
        return out[:13]

    def _handle_faq_payload(self, payload: str) -> Dict[str, Any] | None:
        """
        Quick Reply payload 'FAQ:...' formatındaysa ilgili SSS’i döndürür.
        Post-CTA ekler. Format dışıysa None döner.
        """
        p = (payload or "").strip()
        if not p.upper().startswith("FAQ:"):
            return None
        topic = p.split(":", 1)[1].strip() if ":" in p else ""
        if not topic:
            return None

        base = self.try_faq_reply(topic) or "Bu konuda kayıtlı bir SSS bulunamadı."
        cta_text, cta_qr = self._post_answer_cta_block()
        if cta_text and cta_qr:
            return {"text": f"{base}\n\n{cta_text}", "quick_replies": cta_qr}
        return {"text": base, "quick_replies": self.menu_qr()}

    def menu_qr(self) -> List[Dict[str, str]]:
        """
        Ana giriş seçenekleri YAML’dan gelir (ui.quick_replies).
        Sıralama/başlık/payload tamamen YAML’a aittir.
        """
        return self._qr_from_yaml()

    def _post_answer_cta_block(self) -> tuple[str | None, List[Dict[str, str]] | None]:
        """
        YAML: ui.post_answer_cta:
          text: "Devam etmek ister misiniz?"
          options: ["Paketler & Fiyat", "Randevu / Demo Al", ...]
        Döndür: (cta_text, cta_quick_replies)
        - Eğer tanımlı değilse (None, None) döner.
        """
        ui = self.FLOW.get("ui") or {}
        cfg = ui.get("post_answer_cta") or {}
        text = cfg.get("text")
        options = cfg.get("options") or []
        if not text or not options:
            return None, None
        return text, self._qr_from_titles(options)

    def _human_text(self) -> str:
        """
        YAML: human.in_hours / human.off_hours’tan durumuna göre mesaj döndür.
        """
        human = (self.FLOW.get("human") or {})
        if self.is_business_hours():
            return human.get("in_hours") or "Canlı destek talebinizi ilettik. Birazdan yazacağız."
        else:
            return human.get("off_hours") or "Şu an mesai dışında. İlk iş saatinde yazacağız."

    def _reply(self, text: str, qr: List[Dict[str, str]] | None = None) -> Dict[str, Any]:
        """
        Kısa cevap helper'ı:
        - 'qr' verilirse onu kullanır,
        - verilmezse default olarak ana menü quick reply'lerini ekler.
        """
        return {
            "text": text,
            "quick_replies": (qr if qr is not None else self.menu_qr())
        }


    def lead_next_qr(self) -> List[Dict[str, str]]:
        """Lead akışı sırasında mini kısayollar (opsiyonel)."""
        return [
            self._qr("Devam", "UI:LEAD_CONT"),
            self._qr("Vazgeç", "UI:LEAD_CANCEL"),
        ]

        # ---------- Flow helpers ----------
    def _hours_from_flow(self) -> tuple[int, int]:
        bh = (self.FLOW.get("business_hours") or {})
        try:
            start = int(bh.get("start", 9))
            end   = int(bh.get("end", 18))
        except Exception:
            start, end = 9, 18
        return start, end

    def _offhours_note(self) -> str:
        """
        Mesai dışındayken kısa bir uyarı metni döndürür; mesai içindeyse boş string.
        Tek noktadan yönetim: cümleyi değiştirmek/yerelleştirmek istersen sadece burayı güncelle.
        """
        return "Şu an iş saatleri dışında olabiliriz. " if not self.is_business_hours() else ""

    # ---------- Content Builders (YAML) ----------

    def try_faq_reply(self, text: str) -> str | None:
        faqs = self.FLOW.get("faq") or []
        t = (text or "").lower()
        for f in faqs:
            keys = [k.lower() for k in (f.get("keys") or [])]
            if any(k in t for k in keys):
                start_h, end_h = self._hours_from_flow()
                # 1) {brand}/{cal_link}/{start}/{end
                base = (f.get("reply") or "").format(
                    brand=self.conf_brand(), 
                    cal_link=self.conf_cal_link(),
                    start=start_h,
                    end=end_h
                )
                # 2) {{offhours_note}} gibi çift süslüleri de doldur
                base = self.render_double_brace(base, {
                    "offhours_note": self._offhours_note()
                })
                # CTA (yalın 'lead' beklenir)
                cta = (f.get("cta") or "").strip().lower()
                return base
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
            step = steps[i]
            save_as = step.get("save_as")
            val = (incoming_text or "").strip()

            norm = val.lower()
            if norm in {"devam", "continue", "skip"}:
                return {
                    "text": "Kısa bir yanıt rica edeceğim 🙏 Örneğin birkaç kelime yeterli.",
                    "quick_replies": self.lead_next_qr()
                }

            # 1.a) 'who' ve 'need' için BOŞ mesaja izin verme
            if save_as in ("who","need") and not val:
                return {
                    "text": "Boş bırakmayalım 🙏 Kısaca yazar mısınız?",
                    "quick_replies": self.lead_next_qr()
                }
            
            # 1.b) 'contact' için e-posta/telefon format kontrolü
            if save_as == "contact":
                if not self._looks_like_contact(val):
                    retry_msg = (
                        "Bunu bir iletişim bilgisi olarak anlayamadım. "
                        "Lütfen e-posta (örn: ad@site.com) ya da telefon (örn: +90 5xx xxx xx xx) yazın."
                    )
                    return {
                        "text": retry_msg,
                        "quick_replies": self.lead_next_qr()
                    }

            # 1.c) Buraya geldiysek veri GEÇERLİ → şimdi kaydet
            if save_as:
                self.set_kv(uid, f"lead_data.{save_as}", val)
                s = self.get_session(uid)

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

        off_note = self._offhours_note()

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
            logging.getLogger("igbot").warning(
                "Sheets sink post failed uid=%s who=%r need=%r contact=%r err=%r",
                uid,
                (lead_data.get("who", "")),
                (lead_data.get("need", "")),
                (lead_data.get("contact", "")),
                e,
            )


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
            # ÖNCE: Dinamik SSS payload'larını (FAQ:<keyword>) yakala
            handled = self._handle_faq_payload(quick_payload)
            if handled:
                return handled
            
            # SONRA: Sabit payload → action çöz
            action = (self._payload_to_action(quick_payload) or "").lower()

            if action == "pricing":
                # 'fiyat' SSS’ini çağırıp post-CTA ekle
                base = self.try_faq_reply("fiyat") or "Paket bilgisini yakında güncelleyeceğiz."
                cta_text, cta_qr = self._post_answer_cta_block()
                if cta_text and cta_qr:
                    return {"text": f"{base}\n\n{cta_text}", "quick_replies": cta_qr}
                return self._reply(base)

            if action == "demo":
                txt = f"Takvimden randevu alabilirsiniz: {self.conf_cal_link()}"
                note = self._offhours_note()
                if note:
                    txt += f"\n{note.strip()}"
                
                cta_text, cta_qr = self._post_answer_cta_block()
                if cta_text and cta_qr:
                    return {"text": f"{txt}\n\n{cta_text}", "quick_replies": cta_qr}
                return self._reply(txt)

            if action == "lead":
                return self.start_lead(uid)

            if action == "faq":
                hint = self._faq_hint()
                topics = self._faq_topic_qr(limit=8)  # faq[*].keys[0] → "FAQ:<keyword>" butonları
                if topics:
                    return {"text": f"{hint}\n\nSıkça sorulan başlıklar:", "quick_replies": topics}
                # SSS yoksa ana menüye düş
                return {"text": hint, "quick_replies": self.menu_qr()}

            if action == "human":
                return {"text": self._human_text(), "quick_replies": self.menu_qr()}

            # Tanımsız action → menüye dön
            return self._reply("Nasıl yardımcı olabilirim?")

        # 1) Lead akışı aktifse → ilerlet
        if s["state"] == "lead.active":
            return self.progress_lead(uid, text)

        t = (text or "").strip().lower()

        # 2) Selamlama/İlk temas: doğal bir giriş cümlesi + butonlar
        if any(k in t for k in ["merhaba", "selam", "selamlar", "slm", "hi", "hello", "hey", "iyi günler",
                                "sa","sea","selamun aleykum","selamün aleyküm","günaydın", "iyi akşamlar"
        ]):
            return self._reply(self._greeting_text())

        # 3) SSS anahtar eşleşmesi
        faq = self.try_faq_reply(text)
        if faq:
            cta_text, cta_qr = self._post_answer_cta_block()
            if cta_text and cta_qr:
                return {"text": f"{faq}\n\n{cta_text}", "quick_replies": cta_qr}
            return self._reply(faq)

        # 4) İş saati dışı fallback
        if not self.is_business_hours():
            fb = (self.FLOW.get("fallback") or {}).get("off_hours") or \
                 "Şu an iş saatleri dışında olabiliriz. İsterseniz 'İhtiyacımı İlet' ile bilgilerinizi bırakın, ilk iş dönüş yapalım."
            return {"text": fb, "quick_replies": self.menu_qr()}

        # 5) Genel fallback (iş saati içi)
        fb = (self.FLOW.get("fallback") or {}).get("in_hours") or \
             "Anladım. Aşağıdaki seçeneklerden seçim yapabilirsiniz."
        return {"text": fb, "quick_replies": self.menu_qr()}