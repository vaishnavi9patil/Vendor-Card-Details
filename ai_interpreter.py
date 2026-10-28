"""
Turns OCR text into structured vendor fields with Gemini, then checks
Gemini's answer against the OCR text so nothing invented is saved.
"""

import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field

from destinations import canonical, find_destination_list, mentioned_in_text

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing from your .env file.")

# Main model first; the others are tried when it is overloaded (503) or
# too slow (504). Set GEMINI_MODEL in .env to change the main model.
MODELS = [
    os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
]
# Errors worth retrying: rate limit, server error, overloaded, timeout.
TRANSIENT_CODES = {408, 429, 500, 502, 503, 504}
# Seconds Gemini may take per request before we give up on it
# (main model, then each fallback model).
REQUEST_TIMEOUT = 90
FALLBACK_TIMEOUT = 45

client = genai.Client(
    api_key=API_KEY,
    http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT * 1000),  # ms
)


class AIUnavailable(RuntimeError):
    """Gemini could not be reached / is overloaded right now."""

LIST_FIELDS = (
    "phone_numbers", "emails", "destinations_covered", "websites",
    "other_contacts",
)
TEXT_FIELDS = (
    "company", "person_name", "designation", "address", "services_notes",
)
EXPECTED_FIELDS = [
    "company", "person_name", "designation", "phone_numbers", "emails",
    "destinations_covered", "websites", "address", "services_notes",
    "other_contacts",
]


class VendorCard(BaseModel):
    """The JSON shape Gemini must return."""
    company: str = Field(description="Company / brand name")
    person_name: str = Field(description="Card holder's name")
    designation: str = Field(description="Card holder's job title")
    phone_numbers: list[str] = Field(description="Card holder's and company's general phone numbers")
    emails: list[str] = Field(description="Card holder's and company's general emails")
    destinations_covered: list[str] = Field(description="Destinations the vendor serves, only if explicitly listed")
    websites: list[str]
    address: str
    services_notes: str
    other_contacts: list[str] = Field(description="Other staff listed on the card, one string per person")


PROMPT = """You are extracting a travel vendor's business card into a database.
The text below comes from OCR, one detected line per row. Lines may be
slightly out of order, two-column layouts may be interleaved row by row
(left column first), and OCR may have small mistakes.

Fill the fields as follows:
- company: the company or brand name (e.g. combine "KAZIN" + "DMC" -> "KAZIN DMC").
- person_name / designation: the main card holder (the most prominent name).
  Keep the spelling and capitalisation printed on the card.
- phone_numbers: the card holder's numbers first, then general company
  numbers. Write each as "+<country code> <number>", keeping the digits and
  any "/31" style extensions exactly as printed. Drop labels like "P:", "M:".
- emails / websites: exactly as printed, lower case, no spaces.
- other_contacts: when the card (often the back) lists OTHER staff, put each
  one here as "Name - Role/Region - phone - email" and do NOT put their
  numbers or emails in phone_numbers / emails.
- destinations_covered: only places the vendor explicitly lists as the
  destinations / countries it serves (e.g. a strip "Kazakhstan • Azerbaijan
  • Belarus"). Do NOT include the vendor's own address city or office
  locations. One place per item, standard English spelling.
- address: the main office address as one line.
- services_notes: services, taglines, branch/office locations (e.g.
  "Offices: India, Malaysia, Bali, Vietnam"), social media handles.
  Ignore meaningless OCR fragments.

OCR corrections allowed: fix obvious character confusions (l/1, O/0, I/1,
missing space after a country code) only when the correct value is clear
from the card itself. Never invent information that is not on the card;
use "" or [] when something is missing.

OCR TEXT:
{ocr_text}
"""


# ---------------------------------------------------------------- cleaning

def clean_text(value):
    if value is None:
        return ""
    return " ".join(str(value).split())


def clean_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value] if value.strip() else []
    result = []
    for item in value:
        item = clean_text(item)
        if item and item not in result:
            result.append(item)
    return result


def validate_vendor(data):
    """Make sure every field exists with the right type."""
    if not isinstance(data, dict):
        data = {}
    result = {}
    for field in EXPECTED_FIELDS:
        if field in LIST_FIELDS:
            result[field] = clean_list(data.get(field))
        else:
            result[field] = clean_text(data.get(field))
    return result


# ------------------------------------------------- checks against OCR text

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+\s?@\s?[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LOOKALIKE = str.maketrans({"l": "1", "i": "1", "|": "1", "o": "0"})


def _digits(text):
    return re.sub(r"\D", "", text)


def _loose(text):
    """Lower case, no spaces, l/i/1 and o/0 treated as the same."""
    return re.sub(r"\s", "", text.lower()).translate(_LOOKALIKE)


def _clean_phone(phone):
    phone = re.sub(r"^\s*(?:[A-Za-z]{1,3}\s*[:.]\s*)", "", phone)  # "P:", "Tel."
    phone = re.sub(r"\s*([-/])\s*", r"\1", phone)
    return " ".join(phone.split())


def _phone_on_card(phone, ocr_text):
    digits = _digits(phone)
    if len(digits) < 6:
        return False
    line_digits = [_digits(line) for line in ocr_text.splitlines()]
    all_digits = _digits(ocr_text)
    candidates = [digits, digits[-8:]]
    return any(c in all_digits or any(c in d for d in line_digits) for c in candidates)


def _email_on_card(email, ocr_text):
    return _loose(email) in _loose(ocr_text)


def _website_on_card(site, ocr_text):
    key = re.sub(r"^https?://", "", site.lower()).strip("/")
    key = re.sub(r"^www\.", "", key)
    return bool(key) and _loose(key) in _loose(ocr_text)


def check_against_ocr(vendor, ocr_text):
    """Remove anything Gemini returned that is not on the card, and add
    anything obvious it missed."""
    removed = []

    phones = []
    for phone in vendor["phone_numbers"]:
        phone = _clean_phone(phone)
        if _phone_on_card(phone, ocr_text):
            if phone not in phones:
                phones.append(phone)
        else:
            removed.append(f"phone {phone}")
    vendor["phone_numbers"] = phones

    emails = []
    for email in vendor["emails"]:
        email = email.replace(" ", "").lower()
        if _email_on_card(email, ocr_text):
            if email not in emails:
                emails.append(email)
        else:
            removed.append(f"email {email}")
    vendor["emails"] = emails

    sites = []
    for site in vendor["websites"]:
        site = site.replace(" ", "").lower()
        if _website_on_card(site, ocr_text):
            if site not in sites:
                sites.append(site)
        else:
            removed.append(f"website {site}")
    vendor["websites"] = sites

    # Emails printed on the card that Gemini put nowhere -> keep them
    used = _loose(" ".join(vendor["emails"] + vendor["other_contacts"]))
    for match in _EMAIL_RE.findall(ocr_text):
        email = match.replace(" ", "").lower()
        if _loose(email) not in used:
            vendor["emails"].append(email)
            used += _loose(email)

    # Destinations: keep Gemini's only if they appear on the card, then add
    # any explicit destination list found by the rule-based detector.
    destinations = []
    for place in vendor["destinations_covered"]:
        if mentioned_in_text(place, ocr_text):
            name = canonical(place) or place
            if name not in destinations:
                destinations.append(name)
        else:
            removed.append(f"destination {place}")
    for place in find_destination_list(ocr_text):
        if place not in destinations:
            destinations.append(place)
    vendor["destinations_covered"] = destinations

    if removed:
        print("Removed (not found on card):", ", ".join(removed))
    return vendor


# ------------------------------------------------------------- Gemini call

def _ask_gemini(contents):
    """Ask Gemini, retrying briefly and falling back to other models when
    the main one is overloaded. Raises AIUnavailable if all fail."""
    problems = []

    for index, model in enumerate(dict.fromkeys(MODELS)):   # unique, in order
        timeout = REQUEST_TIMEOUT if index == 0 else FALLBACK_TIMEOUT
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=VendorCard,
            thinking_config=types.ThinkingConfig(thinking_level="LOW"),
            http_options=types.HttpOptions(timeout=timeout * 1000),
        )
        for attempt in range(2):
            start = time.perf_counter()
            try:
                response = client.models.generate_content(
                    model=model, contents=contents, config=config,
                )
                parsed = response.parsed
                if parsed is not None:
                    data = parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
                else:
                    data = json.loads(response.text or "{}")
                print(f"Gemini answered with {model} "
                      f"in {time.perf_counter() - start:.1f}s")
                return data

            except errors.APIError as error:
                code = getattr(error, "code", None)
                problems.append(f"{model}: {code} {getattr(error, 'status', '')}")
                print(f"Gemini {model} attempt {attempt + 1} failed: {code} "
                      f"{getattr(error, 'message', error)}")
                if code not in TRANSIENT_CODES:
                    break                         # e.g. model not found -> next model
                if code in (503, 504):
                    break                         # overloaded/slow -> next model now
                time.sleep(3)

            except (json.JSONDecodeError, ValueError) as error:
                problems.append(f"{model}: bad JSON")
                print(f"Gemini {model} returned unreadable JSON: {error}")

            except Exception as error:            # network down, timeout
                problems.append(f"{model}: {type(error).__name__}")
                print(f"Gemini {model} attempt {attempt + 1} failed: {error}")
                time.sleep(2)

    raise AIUnavailable("Gemini is busy or unreachable (" + "; ".join(problems) + ")")


# ------------------------------------------- fallback without Gemini

_PHONE_RE = re.compile(r"\+?\(?\d[\d\s().-]{6,}\d(?:\s*/\s*\d{1,4})?")
_SITE_RE = re.compile(
    r"\b(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|in|co|travel|"
    r"tours|holidays|info|biz|asia|ae|sg|my|id|uk|us|io|me|global)(?:\.[a-z]{2})?\b",
    re.I,
)


def local_extract(ocr_text):
    """Basic rule-based extraction used when Gemini is unavailable, so the
    scan is not lost. The record is marked for an AI retry."""
    lines = [l.strip() for l in ocr_text.splitlines()
             if l.strip() and not l.startswith("---")]

    emails = []
    for match in _EMAIL_RE.findall(ocr_text):
        email = match.replace(" ", "").lower()
        if email not in emails:
            emails.append(email)

    sites = []
    for line in lines:
        if "@" in line:
            continue
        for match in _SITE_RE.findall(line):
            site = match.lower()
            if site not in sites:
                sites.append(site)

    phones = []
    for line in lines:
        if "@" in line:
            continue
        for match in _PHONE_RE.findall(line):
            if len(_digits(match)) >= 8:
                phone = _clean_phone(match)
                if phone not in phones:
                    phones.append(phone)

    address_lines = [
        l for l in lines
        if l.count(",") >= 2 and re.search(r"\d", l) and "@" not in l
    ]
    # Include the line straight after an address line (e.g. "City, PIN")
    address = []
    for i, line in enumerate(lines):
        if line in address_lines or (i and lines[i - 1] in address_lines and "," in line):
            address.append(line)

    company = ""
    domain_source = (sites[0] if sites else emails[0].split("@")[1] if emails else "")
    if domain_source:
        company = re.sub(r"^(https?://)?(www\.)?", "", domain_source).split(".")[0].upper()

    vendor = validate_vendor({
        "company": company,
        "phone_numbers": phones,
        "emails": emails,
        "websites": sites,
        "destinations_covered": find_destination_list(ocr_text),
        "address": " ".join(address),
        "services_notes": "AI pending - basic extraction only, click Retry AI.",
    })
    return vendor


def interpret_vendor_text(ocr_text):
    """Convert OCR text into structured, verified vendor fields."""
    data = _ask_gemini([PROMPT.format(ocr_text=ocr_text)])
    vendor = validate_vendor(data)
    return check_against_ocr(vendor, ocr_text)


def interpret_vendor_image(image_path):
    """
    Optional direct-image extraction method for future use.
    Does not replace the existing OCR workflow.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Vendor image not found: {image_path}")

    mime_types = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
    }
    mime_type = mime_types.get(image_path.suffix.lower())
    if not mime_type:
        raise ValueError(f"Unsupported image format: {image_path.suffix}")

    prompt = PROMPT.replace(
        "The text below comes from OCR", "The business card image is attached"
    ).split("OCR TEXT:")[0]

    data = _ask_gemini([
        prompt,
        types.Part.from_bytes(data=image_path.read_bytes(), mime_type=mime_type),
    ])
    return validate_vendor(data)
