
import os
import json
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load API key from the project's .env file
PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise RuntimeError(
        "GEMINI_API_KEY was not found in the .env file."
    )

# Use an existing sample business card
image_path = PROJECT_DIR / "sample_cards" / "vendor1_small.jpg"
if not image_path.exists():
    raise FileNotFoundError(
        f"Image not found: {image_path}\n"
        "Check the image filename inside sample_cards."
    )

client = genai.Client(api_key=api_key)

prompt = """
You are extracting vendor contact details from a business card.

Read the image carefully. Return ONLY valid JSON with these fields:

{
  "company": "",
  "person_name": "",
  "designation": "",
  "phone_numbers": [],
  "emails": [],
  "destinations_covered": [],
  "websites": [],
  "address": "",
  "services_notes": ""
}

Rules:
- Copy names, phone numbers, emails, and addresses accurately.
- Keep every phone number and email that is visible.
- Include all destinations or countries explicitly listed.
- Correct obvious OCR-like spelling errors only when the image
  clearly supports the correction.
- Do not invent missing information.
- Use an empty string or empty list for information not visible.
- Put relevant services, brands, or taglines in services_notes.
- Output JSON only, with no markdown or explanation.
"""

print(f"Image: {image_path.name}")
print("Sending image to Gemini...")

start_time = time.time()

try:
    image_bytes = image_path.read_bytes()

    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=[
            prompt,
            types.Part.from_bytes(
                data=image_bytes,
                mime_type="image/jpeg"
            )
        ],
config=types.GenerateContentConfig(
    response_mime_type="application/json",
    thinking_config=types.ThinkingConfig(
        thinking_level="LOW"
        )
    )
)

    print(f"\nTime taken: {time.time() - start_time:.1f} seconds")
    print("\n--- GEMINI RESULT ---")

    result_text = response.text or ""
    result = json.loads(result_text)

    print(json.dumps(result, indent=2, ensure_ascii=False))

    output_path = PROJECT_DIR / "gemini_test_result.json"
    output_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print(f"\nSaved result to: {output_path}")

except Exception as error:
    print(f"\nGemini test failed: {type(error).__name__}: {error}")
