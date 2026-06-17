import asyncio, base64, json, logging, os, re, time
import httpx
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO)
# httpx логирует полный URL на уровне INFO, а Telegram кладёт токен бота прямо в URL —
# глушим его логгер до WARNING, чтобы токен не утекал в логи.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

ai = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

ZAMMAD_BASE  = os.environ["ZAMMAD_BASE_URL"].rstrip("/")
ZAMMAD_TOKEN = os.environ["ZAMMAD_API_TOKEN"]
ZAMMAD_GROUP = os.getenv("ZAMMAD_GROUP", "managers")
FALLBACK_EMAIL = os.getenv("ZAMMAD_FALLBACK_CUSTOMER_EMAIL", "leadbot@example.com")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
try:
    TELEGRAM_COMBINE_WINDOW_SEC = int(os.getenv("TELEGRAM_COMBINE_WINDOW_SEC") or 600)
except (TypeError, ValueError):
    TELEGRAM_COMBINE_WINDOW_SEC = 600

telegram_pending_cards: dict[str, dict] = {}
telegram_manual_state: dict[str, dict] = {}

BTN_MANUAL = "✍️ Ввести вручную"
BTN_CARD = "📷 Визитка"
MAIN_KEYBOARD = {
    "keyboard": [[{"text": BTN_MANUAL}, {"text": BTN_CARD}]],
    "resize_keyboard": True,
    "is_persistent": True,
}
MANUAL_FIELDS = [
    ("name", "Имя? (или «-» чтобы пропустить)"),
    ("company", "Компания?"),
    ("phone", "Телефон?"),
    ("email", "Email?"),
    ("position", "Должность?"),
    ("comment", "Комментарий?"),
]

EXTRACT_SYSTEM = """Ты — ассистент менеджера по продажам на выставке.
Извлеки из текста данные лида. Верни ТОЛЬКО JSON без markdown:
{"name":"...","company":"...","phone":"...","email":"...","position":"...","comment":"..."}
Телефон в международном формате (+7... или +375...). Если поле не найдено — пустая строка."""

CARD_PROMPT = """Это фото визитки. Извлеки все данные.
Верни ТОЛЬКО JSON без markdown:
{"name":"...","company":"...","phone":"...","email":"...","position":"..."}
Телефон в международном формате. Если поля нет — пустая строка."""


def parse_json(raw: str) -> dict:
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return {}


def is_valid_email(value: str) -> bool:
    if not value:
        return False
    # Pragmatic validation for CRM routing; strict RFC parsing is not required here.
    return re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value.strip()) is not None


def guess_mime(filename: str, fallback: str) -> str:
    low = (filename or "").lower()
    if low.endswith(".jpg") or low.endswith(".jpeg"):
        return "image/jpeg"
    if low.endswith(".png"):
        return "image/png"
    if low.endswith(".webp"):
        return "image/webp"
    if low.endswith(".oga") or low.endswith(".ogg"):
        return "audio/ogg"
    if low.endswith(".webm"):
        return "audio/webm"
    if low.endswith(".mp3"):
        return "audio/mpeg"
    if low.endswith(".m4a") or low.endswith(".mp4"):
        return "audio/mp4"
    return fallback


def merge_lead_data(primary: dict, secondary: dict) -> dict:
    merged = dict(primary)
    for key in ["name", "company", "phone", "email", "position"]:
        if not (merged.get(key) or "").strip() and (secondary.get(key) or "").strip():
            merged[key] = secondary.get(key)
    return merged


def append_comment(lead: dict, text: str) -> None:
    base = (lead.get("comment") or "").strip()
    lead["comment"] = f"{base}\n{text}" if base else text


def cleanup_pending_cards() -> None:
    now = time.time()
    for store in (telegram_pending_cards, telegram_manual_state):
        stale = [cid for cid, state in store.items() if now - state.get("ts", 0) > TELEGRAM_COMBINE_WINDOW_SEC]
        for cid in stale:
            store.pop(cid, None)


async def build_card_lead(data: bytes, mime: str, source: str, comment: str = "") -> dict:
    log.info("card: %s bytes, type=%s", len(data), mime)
    lead = await extract_card_lead(data, mime)
    lead.setdefault("source", source)
    if comment:
        append_comment(lead, comment)
    log.info("card lead extracted (fields: %s)", [k for k in ("name", "company", "phone", "email", "position") if (lead.get(k) or "").strip()])
    return lead


def pending_minutes() -> int:
    return max(1, TELEGRAM_COMBINE_WINDOW_SEC // 60)


async def zammad_ticket(lead: dict, attachments: list | None = None) -> dict:
    lines = []
    for key, label in [("name","Имя"),("company","Компания"),("phone","Телефон"),
                       ("email","Email"),("position","Должность"),
                       ("source","Источник"),("comment","Комментарий")]:
        val = lead.get(key, "")
        if val:
            lines.append(f"{label}: {val}")

    company = lead.get("company") or "?"
    name    = lead.get("name")    or "?"
    raw_email = (lead.get("email") or "").strip()
    customer = raw_email if is_valid_email(raw_email) else FALLBACK_EMAIL

    payload = {
        "title": f"Лид: {company} / {name}",
        "group": ZAMMAD_GROUP,
        "customer_id": "guess:" + customer,
        "article": {
            "subject": "Новый лид с выставки",
            "body": "\n".join(lines),
            "type": "note",
            "internal": False,
        },
    }

    atts = [(n, m, d) for (n, m, d) in (attachments or []) if d]
    if atts:
        payload["article"]["attachments"] = [{
            "filename": n or "file",
            "mime-type": m or "application/octet-stream",
            "data": base64.b64encode(d).decode("ascii"),
        } for (n, m, d) in atts]

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"{ZAMMAD_BASE}/api/v1/tickets",
            headers={"Authorization": f"Token token={ZAMMAD_TOKEN}",
                     "Content-Type": "application/json"},
            json=payload,
        )
        if not r.is_success and atts:
            # Some Zammad setups can reject attachment metadata; retry without to avoid losing the lead.
            log.warning("Zammad rejected ticket with attachments (%s): %s. Retrying without.", r.status_code, r.text)
            payload["article"].pop("attachments", None)
            r = await c.post(
                f"{ZAMMAD_BASE}/api/v1/tickets",
                headers={"Authorization": f"Token token={ZAMMAD_TOKEN}",
                         "Content-Type": "application/json"},
                json=payload,
            )
        if not r.is_success:
            log.error("Zammad error %s: %s", r.status_code, r.text)
            r.raise_for_status()
        return r.json()


async def extract_card_lead(data: bytes, mime: str) -> dict:
    b64 = base64.b64encode(data).decode()
    resp = await ai.chat.completions.create(
        model=MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": CARD_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        max_tokens=400,
    )
    raw = resp.choices[0].message.content or ""
    return parse_json(raw)


async def extract_voice_lead(data: bytes, fname: str, mime: str) -> tuple[dict, str]:
    tr = await ai.audio.transcriptions.create(
        model="whisper-1",
        file=(fname, data, mime),
        language="ru",
    )
    text = tr.text
    log.info("transcript received: %d chars", len(text))

    resp = await ai.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": text},
        ],
        max_tokens=400,
    )
    raw = resp.choices[0].message.content or ""
    lead = parse_json(raw)
    if not lead.get("comment"):
        lead["comment"] = text
    return lead, text


async def create_ticket_from_voice(data: bytes, fname: str, mime: str, source: str) -> tuple[dict, int | None, str]:
    log.info("voice: %s bytes, type=%s", len(data), mime)
    lead, text = await extract_voice_lead(data, fname, mime)
    lead.setdefault("source", source)
    log.info("voice lead extracted (fields: %s)", [k for k in ("name", "company", "phone", "email", "position") if (lead.get(k) or "").strip()])

    ticket = await zammad_ticket(lead, attachments=[(fname, mime, data)])
    ticket_id = ticket.get("id")
    return lead, ticket_id, text


async def telegram_api(method: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}", json=payload or {})
        if not r.is_success:
            log.error("Telegram %s error %s: %s", method, r.status_code, r.text)
            return {}
        return r.json()


async def telegram_send_message(chat_id: str | int, text: str, keyboard: dict | None = None) -> None:
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "disable_web_page_preview": True,
    }
    if keyboard is not None:
        payload["reply_markup"] = keyboard
    await telegram_api("sendMessage", payload)


async def telegram_download_file(file_id: str, fallback_name: str, fallback_mime: str) -> tuple[bytes, str, str]:
    meta = await telegram_api("getFile", {"file_id": file_id})
    result = meta.get("result") or {}
    path = result.get("file_path")
    if not path:
        raise RuntimeError("Telegram getFile returned empty file_path")

    filename = path.split("/")[-1] or fallback_name
    mime = guess_mime(filename, fallback_mime)
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{path}"

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url)
        if not r.is_success:
            raise RuntimeError(f"Telegram file download failed: HTTP {r.status_code}")
        return r.content, filename, mime


async def process_update(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message") or {}
    if not msg:
        return

    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if not chat_id:
        return

    cleanup_pending_cards()

    text = (msg.get("text") or "").strip()
    if text.startswith("/start"):
        telegram_pending_cards.pop(chat_id, None)
        telegram_manual_state.pop(chat_id, None)
        await telegram_send_message(
            chat_id,
            "Привет! Выберите действие кнопкой ниже:\n"
            "✍️ Ввести вручную — заполнить данные лида по полям.\n"
            "📷 Визитка — пришлите фото, распознаю; потом можно добавить голос, всё уйдёт в один тикет.\n"
            f"После фото есть {pending_minutes()} мин. Команды: /done, /cancel.",
            keyboard=MAIN_KEYBOARD,
        )
        return

    if text.startswith("/cancel"):
        cancelled = telegram_pending_cards.pop(chat_id, None) or telegram_manual_state.pop(chat_id, None)
        await telegram_send_message(chat_id, "Черновик сброшен." if cancelled else "Активного черновика нет.")
        return

    if text == BTN_MANUAL:
        telegram_pending_cards.pop(chat_id, None)
        telegram_manual_state[chat_id] = {"step": 0, "lead": {}, "ts": time.time()}
        await telegram_send_message(chat_id, f"Ручной ввод лида.\n(1/{len(MANUAL_FIELDS)}) {MANUAL_FIELDS[0][1]}")
        return

    if text == BTN_CARD:
        telegram_manual_state.pop(chat_id, None)
        await telegram_send_message(chat_id, "Пришлите фото визитки 📷 — распознаю автоматически. Потом можно добавить голос.")
        return

    if text.startswith("/done"):
        state = telegram_pending_cards.pop(chat_id, None)
        if not state:
            await telegram_send_message(chat_id, "Нет активной визитки. Сначала отправь фото.")
            return

        lead = state.get("lead") or {}
        lead["source"] = "Telegram / визитка"
        card = state.get("card") or {}
        atts = [(card.get("name"), card.get("mime"), card.get("data"))] if card.get("data") else []
        ticket = await zammad_ticket(lead, attachments=atts)
        ticket_id = ticket.get("id")
        await telegram_send_message(
            chat_id,
            f"Готово. Создан тикет #{ticket_id} (без голоса).\n"
            f"{lead.get('name') or '-'} | {lead.get('company') or '-'} | {lead.get('phone') or '-'}",
        )
        return

    if chat_id in telegram_manual_state and text:
        state = telegram_manual_state[chat_id]
        key = MANUAL_FIELDS[state["step"]][0]
        if text not in ("-", "—", "skip", "/skip"):
            state["lead"][key] = text
        state["step"] += 1
        state["ts"] = time.time()
        if state["step"] < len(MANUAL_FIELDS):
            await telegram_send_message(chat_id, f"({state['step'] + 1}/{len(MANUAL_FIELDS)}) {MANUAL_FIELDS[state['step']][1]}")
            return
        lead = telegram_manual_state.pop(chat_id)["lead"]
        lead["source"] = "Telegram / ручной ввод"
        try:
            ticket = await zammad_ticket(lead)
            ticket_id = ticket.get("id")
            await telegram_send_message(
                chat_id,
                f"Готово. Создан тикет #{ticket_id}.\n"
                f"{lead.get('name') or '-'} | {lead.get('company') or '-'} | {lead.get('phone') or '-'}",
            )
        except Exception:
            log.exception("manual lead ticket failed")
            await telegram_send_message(chat_id, "Не удалось создать тикет. Попробуйте позже.")
        return

    try:
        if msg.get("photo"):
            telegram_manual_state.pop(chat_id, None)
            photos = msg.get("photo") or []
            file_id = photos[-1].get("file_id")
            if not file_id:
                await telegram_send_message(chat_id, "Не удалось прочитать фото. Попробуй еще раз.")
                return

            data, fname, mime = await telegram_download_file(file_id, "card.jpg", "image/jpeg")
            lead = await build_card_lead(
                data=data,
                mime=mime,
                source="Telegram / визитка",
                comment=f"Telegram chat: {chat_id}",
            )
            telegram_pending_cards[chat_id] = {
                "lead": lead,
                "ts": time.time(),
                "card": {"data": data, "name": fname, "mime": mime},
            }
            await telegram_send_message(
                chat_id,
                "Визитка принята. Теперь пришли голосовое сообщение, и я добавлю его в этот же тикет.\n"
                f"Окно ожидания: {pending_minutes()} мин.\n"
                "Если голос не нужен, отправь /done. Для сброса /cancel.\n"
                f"{lead.get('name') or '-'} | {lead.get('company') or '-'} | {lead.get('phone') or '-'}",
            )
            return

        voice = msg.get("voice") or msg.get("audio")
        if voice:
            file_id = voice.get("file_id")
            if not file_id:
                await telegram_send_message(chat_id, "Не удалось прочитать аудио. Попробуй еще раз.")
                return

            data, fname, mime = await telegram_download_file(file_id, "voice.ogg", "audio/ogg")

            state = telegram_pending_cards.pop(chat_id, None)
            if state:
                card_lead = state.get("lead") or {}
                voice_lead, transcript = await extract_voice_lead(data, fname, mime)
                lead = merge_lead_data(card_lead, voice_lead)
                lead["source"] = "Telegram / визитка + голос"
                append_comment(lead, f"Голос (транскрипция): {transcript}")
                card = state.get("card") or {}
                atts = []
                if card.get("data"):
                    atts.append((card.get("name"), card.get("mime"), card.get("data")))
                atts.append((fname, mime, data))
                ticket = await zammad_ticket(lead, attachments=atts)
                ticket_id = ticket.get("id")
                await telegram_send_message(
                    chat_id,
                    f"Готово. Создан один тикет #{ticket_id} (визитка + голос).\n"
                    f"{lead.get('name') or '-'} | {lead.get('company') or '-'} | {lead.get('phone') or '-'}",
                )
                return

            lead, ticket_id, _ = await create_ticket_from_voice(
                data=data,
                fname=fname,
                mime=mime,
                source="Telegram / голос",
            )
            await telegram_send_message(
                chat_id,
                f"Готово. Создан тикет #{ticket_id}.\n"
                f"{lead.get('name') or '-'} | {lead.get('company') or '-'} | {lead.get('phone') or '-'}",
            )
            return

        await telegram_send_message(
            chat_id,
            "Не понял. Нажмите кнопку ниже или /start.",
            keyboard=MAIN_KEYBOARD,
        )
        return

    except Exception:
        log.exception("update handling failed")
        await telegram_send_message(chat_id, "Внутренняя ошибка обработки. Попробуйте позже.")
        return


async def telegram_get_updates(offset: int | None) -> list:
    params = {"timeout": 30, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params=params)
        if not r.is_success:
            log.error("getUpdates error %s: %s", r.status_code, r.text)
            return []
        return (r.json() or {}).get("result") or []


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is empty")
    # Снимаем вебхук, если он был установлен ранее — иначе getUpdates вернёт 409 Conflict.
    await telegram_api("deleteWebhook", {"drop_pending_updates": False})
    log.info("bot started (long polling)")
    offset: int | None = None
    while True:
        try:
            updates = await telegram_get_updates(offset)
        except Exception:
            log.exception("getUpdates failed")
            await asyncio.sleep(3)
            continue
        for upd in updates:
            offset = upd.get("update_id", 0) + 1
            try:
                await process_update(upd)
            except Exception:
                log.exception("update processing failed")


if __name__ == "__main__":
    asyncio.run(main())
