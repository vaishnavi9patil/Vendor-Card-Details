
import os
import base64
import json
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

token = os.getenv("HF_TOKEN")
image_path = Path(__file__).parent / "test_card.jpg"

if not token:
    raise RuntimeError("HF_TOKEN was not found in .env")

if not image_path.exists():
    raise FileNotFoundError(
        f"Image not found: {image_path}\n"
        "Copy the KAZIN DMC card into the project folder "
        "and name it test_card.jpg."
    )

# Resize the test image to reduce upload size.
from PIL import Image

with Image.open(image_path) as img:
    img = img.convert("RGB")
    img.thumbnail((1600, 1600))

    import io
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=88)
    image_bytes = buffer.getvalue()

encoded_image = base64.b64encode(image_bytes).decode("utf-8")
data_url = f"data:image/jpeg;base64,{encoded_image}"

client = OpenAI(
    base_url="https://router.huggingface.co/v1",
    api_key=token,
    timeout=180,
    max_retries=0,
)

prompt = """
Read this business card image and extract the information into JSON.

Return exactly these fields:
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
- Read the image itself, not just imagined or inferred information.
- Inspect the entire card, including small text and bottom strips.
- Include every clearly visible destination country or region.
- Preserve full names, phone numbers, emails, website and address.
- Do not invent information. Use empty strings or arrays if unreadable.
- Return valid JSON only.
"""

print("Sending card to Hugging Face...")
start = time.perf_counter()

try:
    response = client.chat.completions.create(
        model="deepseek-ai/DeepSeek-V4.1-Flash-1017:novita",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            }
        ],
        max_tokens=500,
        temperature=0,
    )

    elapsed = time.perf_counter() - start
    result = response.choices[0].message.content

    print(f"\nCloud response time: {elapsed:.1f} seconds")
    print("\n--- MODEL OUTPUT ---")
    print(result)

    # Check whether the returned text is valid JSON.
    parsed = json.loads(result)
    print("\nJSON validation successful.")
    print("Fields returned:", ", ".join(parsed.keys()))

except Exception as error:
    print(f"\nTest failed: {type(error).__name__}: {error}")
