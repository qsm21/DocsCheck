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
