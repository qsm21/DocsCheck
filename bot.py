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
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, TelegramObject

import config
import gemini_client

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

# Command Handlers
@router.message(Command("start"))
@router.message(Command("check"))
async def cmd_check(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(PassportStates.WAITING_FOR_FILES)
    await state.update_data(passports=[], booking_id=None, crm_tourists=[], booking_info=None)
    await message.answer(
        "👋 Привет! Я бот для сверки паспортных данных с CRM.\n\n"
        "📥 Отправьте мне файлы загранпаспортов туристов (в формате JPG, PNG или многостраничного PDF), "
        "и я распознаю их с помощью Gemini и сверю с CRM-системой."
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
        await callback.message.answer("❌ Ошибка: Выбранная заявка не найдена. Попробуйте еще раз с помощью /check.")
        return
    
    # Save selection details
    await state.set_state(PassportStates.WAITING_FOR_FILES)
    await state.update_data(
        booking_id=booking_id,
        crm_tourists=selected_b["tourists_list"],
        booking_info=selected_b
    )
    
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"🔗 Сессия привязана к заявке **№{selected_b['booking_number']}** ({selected_b['country']} | вылет {selected_b['departure_at']}).\n"
        f"Заказчик: {selected_b['client_name']}.\n"
        f"Отель: {selected_b['hotel']}."
    )
    
    # Run verification check
    await run_composition_and_validation(state, callback.message)

# Document and Photo upload handlers
@router.message(PassportStates.WAITING_FOR_FILES, F.photo | F.document)
async def handle_document_upload(message: Message, state: FSMContext):
    processing_msg = await message.answer("⌛ Скачиваю файл и запускаю распознавание через Gemini...")
    
    pil_images = []
    
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
                # Save PDF to temporary file, then convert pages to images
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(file_bytes.read())
                    tmp_path = tmp.name
                    
                # Convert PDF pages to PIL Images
                pages = convert_from_path(tmp_path)
                pil_images.extend(pages)
                
                # Cleanup temp file
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            elif "image" in mime or doc.file_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                pil_images.append(Image.open(io.BytesIO(file_bytes.read())))
            else:
                await processing_msg.edit_text("❌ Формат файла не поддерживается. Отправьте JPG, PNG или PDF.")
                return

        if not pil_images:
            await processing_msg.edit_text("❌ Не удалось извлечь изображение из файла.")
            return

        # Perform OCR via Gemini
        extracted_passports = []
        warnings = []
        for i, img in enumerate(pil_images):
            if len(pil_images) > 1:
                await processing_msg.edit_text(f"⌛ Распознаю страницу {i+1} из {len(pil_images)}...")
            
            passport_data = gemini_client.extract_passport_data(img)
            if passport_data:
                p_dict = passport_data.model_dump()
                
                # Check for Cyrillic homoglyphs in Latin name fields
                has_homoglyphs = False
                for field in ["first_name_latin", "last_name_latin"]:
                    val = p_dict.get(field) or ""
                    if any(c in CYRILLIC_TO_LATIN_HOMOGLYPHS for c in val):
                        has_homoglyphs = True
                        p_dict[field] = replace_homoglyphs(val)
                
                if has_homoglyphs:
                    warnings.append(
                        f"⚠️ В имени/фамилии **{p_dict['last_name_latin']} {p_dict['first_name_latin']}** были обнаружены русские буквы, замаскированные под латинские (опечатка или ошибка распознавания). Они были автоматически заменены на латинские."
                    )
                
                extracted_passports.append(p_dict)

        if not extracted_passports:
            await processing_msg.edit_text("❌ Gemini не удалось распознать паспортные данные на этом файле. Попробуйте отправить более четкое изображение.")
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
        new_tourists_str = "\n".join([f"👤 **{p['last_name_latin']} {p['first_name_latin']}** (ИИН: {p.get('iin') or 'нет'})" for p in extracted_passports])
        msg_text = f"✅ Распознано новых паспортов ({len(extracted_passports)}):\n{new_tourists_str}"
        if warnings:
            msg_text += "\n\n" + "\n".join(warnings)
        await message.answer(msg_text)
        
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
        "ℹ️ Пожалуйста, сначала введите команду /check или /start, чтобы начать сессию проверки документов."
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
                f"ℹ️ Не найдено ни одной активной заявки для туриста **{first_tourist['last_name_latin']} {first_tourist['first_name_latin']}**.\n"
                "Убедитесь, что заявка заведена в CRM, находится в работе и дата вылета актуальна. Вы можете сбросить сессию командой /check."
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
            await message.answer(
                f"🔗 Автоматическая привязка к заявке **№{selected_b['booking_number']}** ({selected_b['country']} | вылет {selected_b['departure_at']}).\n"
                f"Заказчик: {selected_b['client_name']}.\n"
                f"Отель: {selected_b['hotel']}."
            )
            await run_composition_and_validation(state, message)
        else:
            # Render selection keyboard
            await state.set_state(PassportStates.SELECTING_BOOKING)
            await state.update_data(bookings_list=bookings)
            
            keyboard_buttons = []
            for b in bookings:
                btn_text = f"Заявка №{b['booking_number']} | {b['country']} | {b['departure_at']}"
                keyboard_buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"booking:{b['booking_id']}")])
                
            markup = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            await status_msg.delete()
            await message.answer(
                f"❓ Данный турист найден в нескольких активных заявках ({len(bookings)}). Выберите нужную для проверки:",
                reply_markup=markup
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
        await message.answer(f"⚠️ **Ошибка!** Турист(ы) **{extra_names}** не числятся в заявке №{booking_info['booking_number']}. Проверьте файлы документов.")
        
        # Clean extra passports from session to prevent faulty validation submission
        clean_passports = [p for p in scanned_passports if p not in extra_scans]
        await state.update_data(passports=clean_passports)
        scanned_passports = clean_passports
        
    # 2. Composition Check: missing documents
    missing_tourists = [ct for ct in crm_tourists if ct["id"] not in matched_crm_tourist_ids]
    
    if missing_tourists:
        missing_names = ", ".join([f"**{t['last_name_latin'] or t['last_name']} {t['first_name_latin'] or t['first_name']}**" for t in missing_tourists])
        await message.answer(
            f"📋 В заявке числится туристов: **{len(crm_tourists)}**, но вы прислали только **{len(scanned_passports)}**.\n\n"
            f"Пожалуйста, догрузите паспорт для: {missing_names}.\n\n"
            f"📥 Ожидаю файл..."
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
            
        if response.status_code != 200:
            await message.answer(f"❌ Ошибка финальной сверки ({response.status_code}): {response.text}")
            return
            
        res_data = response.json()
        status = res_data.get("status")
        
        if status == "ok":
            await message.answer(
                "🟢 **Сверка успешна!**\n\n"
                "Все паспортные данные идеально совпали с карточками в CRM.\n"
                "Статус заявки изменен на «Сверено», чекбокс проверки активирован.\n"
                "Системная запись добавлена в историю сделки.",
                parse_mode="Markdown"
            )
            # Reset state for next verification
            await state.clear()
        elif status == "error":
            errors = res_data.get("errors", [])
            errors_str = "\n".join([f"• {e}" for e in errors])
            await message.answer(
                "🔴 **Обнаружены опечатки или расхождения в данных!**\n\n"
                f"{errors_str}\n\n"
                "⚠️ Автоматически создана задача на исправление в CRM (с дедлайном +2 часа для менеджера).\n"
                "Пожалуйста, скорректируйте данные туриста в CRM и запустите проверку заново с помощью кнопки /check.",
                parse_mode="Markdown"
            )
            await state.clear()
        else:
            await message.answer(f"❌ Неизвестный ответ от API: {response.text}")
            
    except Exception as e:
        print(f"Error executing final validation: {e}")
        await message.answer(f"❌ Ошибка связи при валидации: {str(e)}")

# Include router in dp
dp.include_router(router)

async def main():
    print("Starting Telegram Bot...")
    # Delete webhook to make sure polling works
    await bot.delete_webhook(drop_pending_updates=True)
    # Start polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
