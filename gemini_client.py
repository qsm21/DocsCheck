import json
from typing import Optional
from PIL import Image
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
import config

# Define structure for passport extraction
class PassportData(BaseModel):
    first_name_latin: str = Field(description="First name of the tourist in Latin characters as written in passport")
    last_name_latin: str = Field(description="Last name of the tourist in Latin characters as written in passport")
    iin: str = Field(default="", description="12-digit Individual Identification Number (IIN / ИИН) if visible, otherwise empty string")
    passport_number: str = Field(description="Passport number containing only letters and numbers, without spaces or symbols")
    passport_issued_at: str = Field(default="", description="Date of issue in YYYY-MM-DD format if visible, otherwise empty string")
    passport_expires_at: str = Field(default="", description="Date of expiry in YYYY-MM-DD format if visible, otherwise empty string")

# Initialize Gemini Client
client = genai.Client(api_key=config.GEMINI_API_KEY)

def extract_passport_data(image: Image.Image) -> Optional[PassportData]:
    try:
        # Prompt for Gemini Vision
        prompt = (
            "Распознай данные заграничного паспорта владельца. "
            "Извлеки: имя латиницей, фамилию латиницей, ИИН (12 цифр), номер паспорта, дату выдачи и дату окончания действия."
        )

        system_instruction = (
            "You are an expert passport OCR assistant. Extract the passport details of the holder. "
            "Be extremely precise. Convert dates to YYYY-MM-DD format. "
            "Extract name and last name strictly in Latin characters as written in the passport. "
            "Strip any spaces or special characters from the passport number (alphanumeric only). "
            "If IIN (12 digits) is visible in the passport (often under the photo or at the bottom), extract it. "
            "Otherwise, set it to an empty string."
        )

        # Generate content using Gemini 2.5 Flash
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[image, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PassportData,
                system_instruction=system_instruction,
                temperature=0.1
            ),
        )

        if not response.text:
            return None

        # Parse output JSON into Pydantic model
        data = json.loads(response.text)
        return PassportData(**data)
    except Exception as e:
        print(f"Error in Gemini OCR: {e}")
        return None

import pdfplumber
import re

def extract_text_from_pdf(pdf_path: str) -> str:
    """Извлекает текстовый слой из PDF. Возвращает пустую строку если текста нет."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            texts = [page.extract_text() or '' for page in pdf.pages]
            return '\n'.join(texts).strip()
    except Exception as e:
        print(f'pdfplumber error: {e}')
        return ''

def parse_egov_passport_text(text: str) -> Optional[PassportData]:
    """Парсит текстовые данные паспорта РК из eGov PDF без обращения к Gemini."""
    try:
        # ИИН - 12 цифр
        iin_match = re.search(r'\b(\d{12})\b', text)
        iin = iin_match.group(1) if iin_match else ''

        # Номер паспорта: N + 8 цифр или буквы + цифры (например N12345678, AB1234567)
        passport_match = re.search(r'\b([A-Z]{1,2}\d{7,8})\b', text)
        passport_number = passport_match.group(1) if passport_match else ''

        # Даты в формате DD.MM.YYYY или YYYY-MM-DD
        dates = re.findall(r'\b(\d{2}\.\d{2}\.\d{4})\b', text)
        def to_iso(d):
            parts = d.split('.')
            return f'{parts[2]}-{parts[1]}-{parts[0]}'
        issued_at = to_iso(dates[0]) if len(dates) > 0 else ''
        expires_at = to_iso(dates[1]) if len(dates) > 1 else ''

        # Имя и фамилия латиницей (строки из заглавных латинских букв длиной > 2)
        latin_names = re.findall(r'\b([A-Z]{2,})\b', text)
        # Убираем короткие стоп-слова и страну
        stopwords = {'KAZ', 'KZ', 'M', 'F', 'P', 'MRZ', 'UZB'}
        latin_names = [n for n in latin_names if n not in stopwords and len(n) > 2]
        
        last_name = latin_names[0] if len(latin_names) > 0 else ''
        first_name = latin_names[1] if len(latin_names) > 1 else ''

        if not passport_number and not last_name:
            return None

        return PassportData(
            first_name_latin=first_name,
            last_name_latin=last_name,
            iin=iin,
            passport_number=passport_number,
            passport_issued_at=issued_at,
            passport_expires_at=expires_at,
        )
    except Exception as e:
        print(f'eGov parse error: {e}')
        return None

