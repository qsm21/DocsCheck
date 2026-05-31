import os
import io
import sys
from typing import Optional, List, Dict, Any
import tempfile
import asyncio
import httpx
from PIL import Image
from pdf2image import convert_from_path
from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message, 
    CallbackQuery, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    TelegramObject,
    BotCommand,
    ReplyKeyboardMarkup,
    KeyboardButton
)

import config
import gemini_client

# CRM web URL for direct booking links
CRM_WEB_URL = "https://crm.aqtravel.kz"

# Check config variables
if not config.TELEGRAM_BOT_TOKEN or not config.GEMINI_API_KEY:
    print("CRITICAL: Bot token or Gemini API key missing. Exiting.", file=sys.stderr)
    sys.exit(1)

# Middleware for Access Control (based on ALLOWED_USERS)
class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data):
        user = getattr(event, "from_user", None)
        if user:
            if config.ALLOWED_USERS and user.id not in config.ALLOWED_USERS:
                if isinstance(event, Message):
                    await event.answer("❌ Доступ ограничен.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("❌ Доступ ограничен.", show_alert=True)
                return
        return await handler(event, data)

# Initialize Bot and Dispatcher
bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
dp.message.outer_middleware(AccessMiddleware())
dp.callback_query.outer_middleware(AccessMiddleware())
router = Router()

# FSM States
class PassportStates(StatesGroup):
    WAITING_FOR_FILES = State()
    SELECTING_BOOKING = State()

# Transliteration Russian to Latin (ICAO Standard)
RU_TO_LATIN = {
    'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'E', 'Ж': 'ZH', 'З': 'Z', 'И': 'I', 'Й': 'Y',
    'К': 'K', 'Л': 'L', 'М': 'M', 'Н': 'N', 'О': 'O', 'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U', 'Ф': 'F',
    'Х': 'KH', 'Ц': 'TS', 'Ч': 'CH', 'Ш': 'SH', 'Щ': 'SHCH', 'Ъ': '', 'Ы': 'Y', 'Ь': '', 'Э': 'E', 'Ю': 'YU', 'Я': 'YA',
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e', 'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y',
    'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f',
    'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch', 'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'
}

CYRILLIC_TO_LATIN_HOMOGLYPHS = {
    'А': 'A', 'В': 'B', 'Е': 'E', 'К': 'K', 'М': 'M', 'Н': 'H', 'О': 'O', 'Р': 'P', 'С': 'C', 'Т': 'T', 'У': 'Y', 'Х': 'X',
    'а': 'a', 'е': 'e', 'о': 'o', 'р': 'p', 'с': 'c', 'у': 'y', 'х': 'x'
}

def replace_homoglyphs(s: str) -> str:
    return "".join(CYRILLIC_TO_LATIN_HOMOGLYPHS.get(c, c) for c in s)

def transliterate(s: str) -> str:
    return "".join(RU_TO_LATIN.get(c, c) for c in s)

# KZ passport numbers may contain country prefix KAZN or KAZ — strip it
def normalize_passport_number(num: Optional[str]) -> str:
    if not num:
        return ""
    upper = num.upper().strip()
    # Remove leading KAZN or KAZ (Kazakhstan country code prefix)
    for prefix in ("KAZN", "KAZ"):
        if upper.startswith(prefix):
            upper = upper[len(prefix):]
            break
    return upper

def normalize_name(s: Optional[str]) -> str:
    if not s:
        return ""
    cleaned = replace_homoglyphs(s)
    return "".join(c for c in cleaned.upper() if c.isalpha())

# Match passport scan to CRM tourist
def find_matching_crm_tourist(scan, crm_tourists):
    scan_last = normalize_name(scan.get("last_name_latin"))
    scan_first = normalize_name(scan.get("first_name_latin"))
    
    # Try match by IIN first
    scan_iin = "".join(c for c in scan.get("iin", "") if c.isdigit())
    if scan_iin:
        for ct in crm_tourists:
            crm_iin = "".join(c for c in (ct.get("iin") or "") if c.isdigit())
            if crm_iin == scan_iin:
                return ct
                
    # Try match by Name
    for ct in crm_tourists:
        # Check Latin name if filled
        crm_last_lat = ct.get("last_name_latin")
        crm_first_lat = ct.get("first_name_latin")
        
        # If Latin names not filled, transliterate Russian names
        if not crm_last_lat:
            crm_last_lat = transliterate(ct.get("last_name") or "")
        if not crm_first_lat:
            crm_first_lat = transliterate(ct.get("first_name") or "")
            
        crm_last = normalize_name(crm_last_lat)
        crm_first = normalize_name(crm_first_lat)
        
        if crm_last == scan_last and crm_first == scan_first:
            return ct
            
    return None

def main_keyboard() -> ReplyKeyboardMarkup:
    """Обычная клавиатура под полем ввода с кнопкой Сбросить/Начать проверку"""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔄 Начать новую проверку")]],
        resize_keyboard=True,
        one_time_keyboard=False
    )

def new_check_keyboard(booking_id: Optional[int] = None) -> InlineKeyboardMarkup:
    """Inline keyboard with a button to start a new verification.
    Optionally includes a direct CRM link for the current booking.
    """
    buttons = []
    if booking_id:
        crm_url = f"{CRM_WEB_URL}/dashboard?booking={booking_id}"
        buttons.append([InlineKeyboardButton(text="🔗 Открыть в CRM", url=crm_url)])
    buttons.append([InlineKeyboardButton(text="🔄 Новая проверка", callback_data="new_check")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Callback: start new check via inline button
@router.callback_query(F.data == "new_check")
async def cb_new_check(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await state.set_state(PassportStates.WAITING_FOR_FILES)
    await state.update_data(passports=[], booking_id=None, crm_tourists=[], booking_info=None)
    await callback.message.answer(
        "🔄 <b>Сессия сброшена.</b> Бот готов к новой проверке!\n\n"
        "📥 Отправьте файлы загранпаспортов туристов (<b>JPG, PNG или PDF</b>).",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

# Handle text reply button "Начать новую проверку"
@router.message(F.text == "🔄 Начать новую проверку")
async def btn_new_check(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(PassportStates.WAITING_FOR_FILES)
    await state.update_data(passports=[], booking_id=None, crm_tourists=[], booking_info=None)
    await message.answer(
        "🔄 <b>Сессия сброшена.</b> Бот готов к новой проверке!\n\n"
        "📥 Отправьте файлы загранпаспортов туристов (<b>JPG, PNG или PDF</b>).",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

# Command Handlers
@router.message(Command("start"))
@router.message(Command("check"))
async def cmd_check(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(PassportStates.WAITING_FOR_FILES)
    await state.update_data(passports=[], booking_id=None, crm_tourists=[], booking_info=None)
    await message.answer(
        "👋 <b>Привет! Я бот для сверки паспортных данных с CRM.</b>\n\n"
        "📥 Отправьте мне файлы загранпаспортов туристов (в формате <b>JPG, PNG или многостраничного PDF</b>).\n\n"
        "⚡ Бот автоматически определит eGov PDF или фото и сверит данные с системой.",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

# Callback handler for selecting booking from CRM list
@router.callback_query(PassportStates.SELECTING_BOOKING, F.data.startswith("booking:"))
async def process_booking_selection(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    booking_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    bookings_list = data.get("bookings_list", [])
    
    # Find details of selected booking
    selected_b = next((b for b in bookings_list if b["booking_id"] == booking_id), None)
    if not selected_b:
        await callback.message.answer(
            "❌ <b>Ошибка:</b> Выбранная заявка не найдена. Нажмите кнопку внизу или введите /check.",
            parse_mode="HTML"
        )
        return
    
    # Save selection details
    await state.set_state(PassportStates.WAITING_FOR_FILES)
    await state.update_data(
        booking_id=booking_id,
        crm_tourists=selected_b["tourists_list"],
        booking_info=selected_b
    )
    
    await callback.message.edit_reply_markup(reply_markup=None)
    crm_url = f"{CRM_WEB_URL}/dashboard?booking={booking_id}"
    await callback.message.answer(
        f"🔗 <b>Сессия успешно привязана к заявке №{selected_b['booking_number']}</b>\n"
        f"📍 Страна: <b>{selected_b['country']}</b>\n"
        f"📅 Вылет: <b>{selected_b['departure_at']}</b>\n"
        f"👤 Заказчик: <b>{selected_b['client_name']}</b>\n"
        f"🏨 Отель: <b>{selected_b['hotel']}</b>\n\n"
        f"🧭 <a href='{crm_url}'>Открыть эту заявку в CRM</a>",
        parse_mode="HTML"
    )
    
    # Run verification check
    await run_composition_and_validation(state, callback.message)

# Document and Photo upload handlers
@router.message(PassportStates.WAITING_FOR_FILES, F.photo | F.document)
async def handle_document_upload(message: Message, state: FSMContext):
    processing_msg = await message.answer("⌛ Скачиваю файл и запускаю распознавание...")
    
    pil_images = []
    extracted_passports_direct = []
    
    try:
        if message.photo:
            # Handle photo directly
            photo = message.photo[-1]
            file_info = await bot.get_file(photo.file_id)
            file_bytes = await bot.download_file(file_info.file_path)
            pil_images.append(Image.open(io.BytesIO(file_bytes.read())))
            
        elif message.document:
            # Handle document (PDF, JPG, PNG)
            doc = message.document
            mime = doc.mime_type or ""
            file_info = await bot.get_file(doc.file_id)
            file_bytes = await bot.download_file(file_info.file_path)
            
            if "pdf" in mime or doc.file_name.lower().endswith(".pdf"):
                # Save PDF to temporary file
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(file_bytes.read())
                    tmp_path = tmp.name

                # Try text extraction first (eGov KZ PDFs with selectable text)
                pdf_text = gemini_client.extract_text_from_pdf(tmp_path)
                if pdf_text and len(pdf_text) > 50:
                    # Text layer found — parse without Gemini
                    await processing_msg.edit_text("⚡ <b>Обнаружен цифровой eGov PDF!</b>\nИзвлекаю данные напрямую без OCR...")
                    passport_data = gemini_client.parse_egov_passport_text(pdf_text)
                    if passport_data:
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass
                        # Use existing processing pipeline with extracted data
                        pil_images = [None]  # Sentinel to signal text-mode
                        extracted_passports_direct = [passport_data]
                    else:
                        # Text found but could not parse — fall back to OCR
                        pages = convert_from_path(tmp_path)
                        pil_images.extend(pages)
                        extracted_passports_direct = []
                else:
                    # No text layer — use OCR via Gemini
                    pages = convert_from_path(tmp_path)
                    pil_images.extend(pages)
                    extracted_passports_direct = []

                # Cleanup temp file
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            elif "image" in mime or doc.file_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                pil_images.append(Image.open(io.BytesIO(file_bytes.read())))
            else:
                await processing_msg.edit_text("❌ <b>Формат файла не поддерживается.</b>\nОтправьте JPG, PNG или PDF.")
                return

        if not pil_images:
            await processing_msg.edit_text("❌ Не удалось извлечь изображение из файла.")
            return

        # Perform OCR via Gemini (or use directly extracted text passports)
        extracted_passports = []
        warnings = []

        # If text extraction succeeded — skip OCR loop entirely
        if extracted_passports_direct:
            extracted_passports = [p.model_dump() for p in extracted_passports_direct]
        else:
            extracted_passports_direct = []

        for i, img in enumerate(pil_images):
            if img is None:  # Sentinel: skip OCR for text-mode PDFs
                continue
            if len(pil_images) > 1:
                await processing_msg.edit_text(f"⌛ Распознаю страницу <b>{i+1}</b> из <b>{len(pil_images)}</b> через Gemini...")
            
            passport_data = gemini_client.extract_passport_data(img)
            if passport_data:
                p_dict = passport_data.model_dump()
                
                # Normalize passport number: strip KAZ/KAZN country prefix
                raw_num = p_dict.get("passport_number") or ""
                normalized_num = normalize_passport_number(raw_num)
                if normalized_num != raw_num:
                    warnings.append(
                        f"ℹ️ Номер паспорта <b>{raw_num}</b> — убран префикс страны, используется <b>{normalized_num}</b>."
                    )
                p_dict["passport_number"] = normalized_num
                
                # Check for Cyrillic homoglyphs in Latin name fields
                has_homoglyphs = False
                for field in ["first_name_latin", "last_name_latin"]:
                    val = p_dict.get(field) or ""
                    if any(c in CYRILLIC_TO_LATIN_HOMOGLYPHS for c in val):
                        has_homoglyphs = True
                        p_dict[field] = replace_homoglyphs(val)
                
                if has_homoglyphs:
                    warnings.append(
                        f"⚠️ В имени/фамилии <b>{p_dict['last_name_latin']} {p_dict['first_name_latin']}</b> были обнаружены русские буквы, замаскированные под латинские. Они были автоматически заменены."
                    )
                
                extracted_passports.append(p_dict)

        if not extracted_passports:
            await processing_msg.edit_text("❌ <b>Gemini не удалось распознать паспортные данные.</b>\nПопробуйте прислать более четкое изображение.")
            return

        # Save to state
        data = await state.get_data()
        current_passports = data.get("passports", [])
        
        # Add new scans
        for p in extracted_passports:
            # Check if name is already added to avoid duplicates in the same session
            if not any(normalize_name(x["first_name_latin"]) == normalize_name(p["first_name_latin"]) and normalize_name(x["last_name_latin"]) == normalize_name(p["last_name_latin"]) for x in current_passports):
                current_passports.append(p)
                
        await state.update_data(passports=current_passports)
        
        # Format list of successfully extracted tourists
        new_tourists_str = "\n".join([f"👤 <b>{p['last_name_latin']} {p['first_name_latin']}</b> (ИИН: <code>{p.get('iin') or 'нет'}</code>)" for p in extracted_passports])
        msg_text = f"✅ <b>Распознано новых паспортов ({len(extracted_passports)}):</b>\n{new_tourists_str}"
        if warnings:
            msg_text += "\n\n" + "\n".join(warnings)
        await message.answer(msg_text, parse_mode="HTML")
        
        # If booking is not linked yet, search CRM
        booking_id = data.get("booking_id")
        if not booking_id:
            await processing_msg.edit_text("🔍 Ищу подходящие активные заявки в CRM...")
            await search_crm_and_link(state, message, extracted_passports[0], processing_msg)
        else:
            await processing_msg.delete()
            await run_composition_and_validation(state, message)

    except Exception as e:
        print(f"Error handling file upload: {e}")
        await processing_msg.edit_text(f"❌ Произошла ошибка во время обработки файла: {str(e)}")

# Handler for documents sent outside of FSM state
@router.message(F.photo | F.document)
async def handle_document_outside_state(message: Message):
    await message.answer(
        "ℹ️ Пожалуйста, нажмите кнопку <b>🔄 Начать новую проверку</b> ниже, чтобы запустить сессию проверки документов.",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

# Search CRM by first tourist data and link booking
async def search_crm_and_link(state: FSMContext, message: Message, first_tourist: dict, status_msg: Message):
    payload = {
        "iin": first_tourist.get("iin"),
        "name": f"{first_tourist['last_name_latin']} {first_tourist['first_name_latin']}"
    }
    
    headers = {"x-api-key": config.CRM_API_KEY}
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{config.CRM_API_URL}/booking/search", json=payload, headers=headers, timeout=15.0)
            
        if response.status_code != 200:
            await status_msg.edit_text(f"❌ Ошибка подключения к CRM ({response.status_code}): {response.text}")
            return
            
        res_data = response.json()
        if not res_data.get("success"):
            await status_msg.edit_text(f"❌ Ошибка поиска в CRM: {res_data.get('error')}")
            return
            
        bookings = res_data.get("bookings", [])
        
        if len(bookings) == 0:
            await status_msg.edit_text(
                f"🔍 <b>Не найдено активных заявок</b> для туриста <b>{first_tourist['last_name_latin']} {first_tourist['first_name_latin']}</b>.\n\n"
                "Убедитесь, что заявка создана в CRM, находится в работе и дата вылета актуальна.\n\n"
                "Вы можете сбросить сессию кнопкой <b>🔄 Начать новую проверку</b>.",
                parse_mode="HTML"
            )
        elif len(bookings) == 1:
            # Bind automatically
            selected_b = bookings[0]
            await state.update_data(
                booking_id=selected_b["booking_id"],
                crm_tourists=selected_b["tourists_list"],
                booking_info=selected_b
            )
            await status_msg.delete()
            crm_url = f"{CRM_WEB_URL}/dashboard?booking={selected_b['booking_id']}"
            await message.answer(
                f"🔗 <b>Автоматическая привязка к заявке №{selected_b['booking_number']}</b>\n"
                f"📍 Страна: <b>{selected_b['country']}</b> | Вылет: <b>{selected_b['departure_at']}</b>\n"
                f"👤 Заказчик: <b>{selected_b['client_name']}</b>\n"
                f"🏨 Отель: <b>{selected_b['hotel']}</b>\n\n"
                f"🧭 <a href='{crm_url}'>Открыть заявку в CRM</a>",
                parse_mode="HTML"
            )
            await run_composition_and_validation(state, message)
        else:
            # Render selection keyboard
            await state.set_state(PassportStates.SELECTING_BOOKING)
            await state.update_data(bookings_list=bookings)
            
            keyboard_buttons = []
            for b in bookings:
                btn_text = f"№{b['booking_number']} | {b['country']} | {b['departure_at']}"
                keyboard_buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"booking:{b['booking_id']}")])
                
            markup = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            await status_msg.delete()
            await message.answer(
                f"❓ Данный турист найден в <b>{len(bookings)}</b> активных заявках. Выберите нужную для сверки:",
                reply_markup=markup,
                parse_mode="HTML"
            )
            
    except Exception as e:
        print(f"Error querying CRM: {e}")
        await status_msg.edit_text(f"❌ Не удалось связаться с CRM: {str(e)}")

# Perform Composition Check and Validate details
async def run_composition_and_validation(state: FSMContext, message: Message):
    data = await state.get_data()
    scanned_passports = data.get("passports", [])
    crm_tourists = data.get("crm_tourists", [])
    booking_info = data.get("booking_info", {})
    booking_id = data.get("booking_id")
    
    # 1. Composition Check: extra documents
    extra_scans = []
    matched_crm_tourist_ids = set()
    
    for scan in scanned_passports:
        matched = find_matching_crm_tourist(scan, crm_tourists)
        if matched:
            matched_crm_tourist_ids.add(matched["id"])
        else:
            extra_scans.append(scan)
            
    if extra_scans:
        extra_names = ", ".join([f"{p['last_name_latin']} {p['first_name_latin']}" for p in extra_scans])
        await message.answer(
            f"⚠️ <b>Ошибка состава туристов!</b>\n"
            f"Турист(ы) <b>{extra_names}</b> не числятся в заявке №{booking_info['booking_number']}.\n\n"
            f"Проверьте правильность отправленных файлов.",
            parse_mode="HTML"
        )
        
        # Clean extra passports from session to prevent faulty validation submission
        clean_passports = [p for p in scanned_passports if p not in extra_scans]
        await state.update_data(passports=clean_passports)
        scanned_passports = clean_passports
        
    # 2. Composition Check: missing documents
    missing_tourists = [ct for ct in crm_tourists if ct["id"] not in matched_crm_tourist_ids]
    
    if missing_tourists:
        missing_names = ", ".join([f"<b>{t['last_name_latin'] or t['last_name']} {t['first_name_latin'] or t['first_name']}</b>" for t in missing_tourists])
        await message.answer(
            f"📋 В CRM-заявке туристов: <b>{len(crm_tourists)}</b>, но получено документов только: <b>{len(scanned_passports)}</b>.\n\n"
            f"⌛ <b>Ожидаю паспорт для:</b> {missing_names}\n\n"
            f"📥 Дошлите следующий файл...",
            parse_mode="HTML"
        )
        return
        
    # 3. Final validation when lists match perfectly
    await message.answer("🔄 Все документы получены. Запускаю финальную сверку полей в CRM...")
    
    payload = {
        "booking_id": booking_id,
        "passports": scanned_passports
    }
    
    headers = {"x-api-key": config.CRM_API_KEY}
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{config.CRM_API_URL}/booking/validate", json=payload, headers=headers, timeout=20.0)
        
        # Always log the raw CRM response for debugging
        print(f"[validate] HTTP {response.status_code} | body: {response.text[:500]}")
            
        if response.status_code != 200:
            await message.answer(
                f"❌ <b>Ошибка финальной сверки (HTTP {response.status_code}):</b>\n<code>{response.text[:300]}</code>",
                parse_mode="HTML"
            )
            return
            
        res_data = response.json()
        status = res_data.get("status")
        
        # Build comparative report
        report_parts = []
        for ct in crm_tourists:
            # Find matching scan
            matched_scan = None
            crm_iin = "".join(c for c in (ct.get("iin") or "") if c.isdigit())
            if crm_iin:
                matched_scan = next((s for s in scanned_passports if "".join(c for c in (s.get("iin") or "") if c.isdigit()) == crm_iin), None)
            if not matched_scan:
                crm_last = normalize_name(ct.get("last_name_latin") or transliterate(ct.get("last_name") or ""))
                crm_first = normalize_name(ct.get("first_name_latin") or transliterate(ct.get("first_name") or ""))
                matched_scan = next((s for s in scanned_passports if normalize_name(s.get("last_name_latin")) == crm_last and normalize_name(s.get("first_name_latin")) == crm_first), None)
            
            if matched_scan:
                def comp(field_name, val_crm, val_scan, is_name=False):
                    c_crm = (val_crm or "").strip().upper()
                    c_scan = (val_scan or "").strip().upper()
                    if is_name:
                        c_crm = normalize_name(val_crm)
                        c_scan = normalize_name(val_scan)
                    else:
                        c_crm = "".join(c for c in c_crm if c.isalnum())
                        c_scan = "".join(c for c in c_scan if c.isalnum())
                    
                    if c_crm == c_scan:
                        return f"• {field_name}: <code>{val_crm or '—'}</code> ✅"
                    else:
                        return f"• {field_name}: ❌ CRM <code>{val_crm or '—'}</code> ↔️ Паспорт <code>{val_scan or '—'}</code>"
                
                tourist_name = f"{ct.get('last_name_latin') or ct.get('last_name') or ''} {ct.get('first_name_latin') or ct.get('first_name') or ''}".strip().upper()
                report_parts.append(
                    f"👤 <b>{tourist_name}</b>:\n"
                    f"{comp('Фамилия (lat)', ct.get('last_name_latin'), matched_scan.get('last_name_latin'), is_name=True)}\n"
                    f"{comp('Имя (lat)', ct.get('first_name_latin'), matched_scan.get('first_name_latin'), is_name=True)}\n"
                    f"{comp('Номер паспорта', ct.get('passport_number'), matched_scan.get('passport_number'))}\n"
                    f"{comp('ИИН', ct.get('iin'), matched_scan.get('iin'))}\n"
                    f"{comp('Срок действия', ct.get('passport_expires_at'), matched_scan.get('passport_expires_at'))}"
                )
            else:
                tourist_name = f"{ct.get('last_name') or ''} {ct.get('first_name') or ''}".strip().upper()
                report_parts.append(f"👤 <b>{tourist_name}</b>:\n• ❌ Отсутствует скан паспорта!")
        
        comparison_str = "\n\n".join(report_parts)
        
        if status == "ok":
            await state.clear()
            await message.answer(
                "🟢 <b>Сверка успешна!</b>\n\n"
                "Все паспортные данные идеально совпали с карточками в CRM.\n\n"
                f"{comparison_str}\n\n"
                "Статус заявки изменен на «Сверено», чекбокс проверки активирован.\n"
                "История сделки обновлена.",
                parse_mode="HTML",
                reply_markup=new_check_keyboard(booking_id)
            )
        elif status == "error":
            errors = res_data.get("errors", [])
            errors_str = "\n".join([f"• {e}" for e in errors])
            await state.clear()
            await message.answer(
                "🔴 <b>Обнаружены расхождения в данных!</b>\n\n"
                f"{comparison_str}\n\n"
                f"<b>Ошибки сверки:</b>\n{errors_str}\n\n"
                "⚠️ Автоматически создана задача на исправление в CRM (с дедлайном 2 часа).\n"
                "Скорректируйте данные в CRM и нажмите кнопку ниже для повторной проверки.",
                parse_mode="HTML",
                reply_markup=new_check_keyboard(booking_id)
            )
        else:
            await message.answer(
                f"❌ <b>Неизвестный ответ от API:</b>\n<code>{response.text[:300]}</code>",
                parse_mode="HTML",
                reply_markup=new_check_keyboard(booking_id)
            )
            
    except Exception as e:
        print(f"Error executing final validation: {e}")
        await message.answer(
            f"❌ Ошибка связи при валидации: {str(e)}",
            reply_markup=new_check_keyboard()
        )

# Include router in dp
dp.include_router(router)

async def main():
    print("Starting Telegram Bot...")
    
    # Регистрация меню команд в Telegram
    await bot.set_my_commands([
        BotCommand(command="check", description="🔄 Начать/сбросить сверку документов"),
        BotCommand(command="start", description="👋 Перезапустить бота")
    ])
    
    # Delete webhook to make sure polling works
    await bot.delete_webhook(drop_pending_updates=True)
    # Start polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
