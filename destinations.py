"""
Destination detection that works for any card, not just KAZIN.

- Knows all countries plus common travel destinations (cities, islands,
  regions, Indian states).
- Splits names that OCR has joined together ("ArmeniaUzbekistan",
  "KAZAKHSTANAZERBAIJAN").
- Tolerates small OCR spelling mistakes ("Azerbaljan" -> "Azerbaijan").
- Only treats a line as a destination list when it is mostly place names,
  so an address such as "Gurugram, Haryana" or a label such as
  "India Office" is not mistaken for a destinations list.
"""

import difflib
import re

# Canonical name -> extra spellings / aliases
COUNTRIES = {
    "Afghanistan": [], "Albania": [], "Algeria": [], "Andorra": [],
    "Angola": [], "Antigua and Barbuda": ["Antigua"], "Argentina": [],
    "Armenia": [], "Australia": [], "Austria": [], "Azerbaijan": [],
    "Bahamas": [], "Bahrain": [], "Bangladesh": [], "Barbados": [],
    "Belarus": [], "Belgium": [], "Belize": [], "Benin": [], "Bhutan": [],
    "Bolivia": [], "Bosnia and Herzegovina": ["Bosnia"], "Botswana": [],
    "Brazil": [], "Brunei": [], "Bulgaria": [], "Burkina Faso": [],
    "Burundi": [], "Cambodia": [], "Cameroon": [], "Canada": [],
    "Cape Verde": [], "Chad": [], "Chile": [], "China": [],
    "Colombia": [], "Comoros": [], "Congo": [], "Costa Rica": [],
    "Croatia": [], "Cuba": [], "Cyprus": [],
    "Czech Republic": ["Czechia"], "Denmark": [], "Djibouti": [],
    "Dominica": [], "Dominican Republic": [], "Ecuador": [], "Egypt": [],
    "El Salvador": [], "Estonia": [], "Eswatini": [], "Ethiopia": [],
    "Fiji": [], "Finland": [], "France": [], "Gabon": [], "Gambia": [],
    "Georgia": [], "Germany": [], "Ghana": [], "Greece": [],
    "Grenada": [], "Guatemala": [], "Guinea": [], "Guyana": [],
    "Haiti": [], "Honduras": [], "Hungary": [], "Iceland": [],
    "India": [], "Indonesia": [], "Iran": [], "Iraq": [], "Ireland": [],
    "Israel": [], "Italy": [], "Ivory Coast": [], "Jamaica": [],
    "Japan": [], "Jordan": [], "Kazakhstan": [], "Kenya": [],
    "Kuwait": [], "Kyrgyzstan": [], "Laos": [], "Latvia": [],
    "Lebanon": [], "Lesotho": [], "Liberia": [], "Libya": [],
    "Liechtenstein": [], "Lithuania": [], "Luxembourg": [],
    "Madagascar": [], "Malawi": [], "Malaysia": [], "Maldives": [],
    "Mali": [], "Malta": [], "Mauritania": [], "Mauritius": [],
    "Mexico": [], "Moldova": [], "Monaco": [], "Mongolia": [],
    "Montenegro": [], "Morocco": [], "Mozambique": [],
    "Myanmar": ["Burma"], "Namibia": [], "Nepal": [], "Netherlands": ["Holland"],
    "New Zealand": [], "Nicaragua": [], "Niger": [], "Nigeria": [],
    "North Macedonia": ["Macedonia"], "Norway": [], "Oman": [],
    "Pakistan": [], "Panama": [], "Papua New Guinea": [], "Paraguay": [],
    "Peru": [], "Philippines": [], "Poland": [], "Portugal": [],
    "Qatar": [], "Romania": [], "Russia": [], "Rwanda": [],
    "Saint Lucia": ["St Lucia"], "Samoa": [], "San Marino": [],
    "Saudi Arabia": ["KSA"], "Senegal": [], "Serbia": [],
    "Seychelles": [], "Sierra Leone": [], "Singapore": [],
    "Slovakia": [], "Slovenia": [], "Somalia": [], "South Africa": [],
    "South Korea": ["Korea"], "Spain": [], "Sri Lanka": ["Srilanka"],
    "Sudan": [], "Suriname": [], "Sweden": [], "Switzerland": [],
    "Syria": [], "Taiwan": [], "Tajikistan": [], "Tanzania": [],
    "Thailand": [], "Timor-Leste": ["East Timor"], "Togo": [],
    "Tonga": [], "Trinidad and Tobago": [], "Tunisia": [],
    "Turkey": ["Turkiye", "Türkiye"], "Turkmenistan": [], "Uganda": [],
    "Ukraine": [], "UAE": ["United Arab Emirates", "U.A.E"],
    "UK": ["United Kingdom", "Great Britain", "England", "Scotland", "Wales"],
    "USA": ["United States", "United States of America", "U.S.A"],
    "Uruguay": [], "Uzbekistan": [], "Vanuatu": [], "Vatican": [],
    "Venezuela": [], "Vietnam": ["Viet Nam"], "Yemen": [],
    "Zambia": [], "Zimbabwe": [],
    "Hong Kong": [], "Macau": ["Macao"], "Puerto Rico": [],
    "Greenland": [], "Tibet": [],
}

# Popular travel destinations that are not countries
PLACES = [
    # Regions
    "Europe", "Scandinavia", "Balkans", "Baltics", "Caucasus",
    "Central Asia", "Southeast Asia", "South East Asia", "Far East",
    "Middle East", "Africa", "East Africa", "South America",
    "Latin America", "North America", "Caribbean", "Oceania",
    "CIS", "GCC", "Indian Ocean", "Asia",
    # Middle East / Asia cities and islands
    "Dubai", "Abu Dhabi", "Sharjah", "Ras Al Khaimah", "Doha", "Muscat",
    "Riyadh", "Jeddah", "Bali", "Lombok", "Jakarta", "Bangkok", "Phuket",
    "Pattaya", "Krabi", "Koh Samui", "Chiang Mai", "Kuala Lumpur",
    "Langkawi", "Penang", "Genting", "Sabah", "Borneo", "Hanoi",
    "Ho Chi Minh", "Da Nang", "Halong Bay", "Siem Reap", "Phnom Penh",
    "Tokyo", "Kyoto", "Osaka", "Seoul", "Beijing", "Shanghai",
    "Manila", "Boracay", "Cebu", "Kathmandu", "Pokhara", "Colombo",
    "Male", "Baku", "Tbilisi", "Batumi", "Yerevan", "Almaty", "Astana",
    "Tashkent", "Samarkand", "Bukhara", "Bishkek", "Minsk", "Moscow",
    "St Petersburg", "Istanbul", "Cappadocia", "Antalya",
    # Europe / elsewhere
    "Paris", "London", "Rome", "Venice", "Milan", "Barcelona", "Madrid",
    "Amsterdam", "Prague", "Vienna", "Budapest", "Zurich", "Lucerne",
    "Interlaken", "Santorini", "Mykonos", "Athens", "Lisbon", "Reykjavik",
    "Lapland", "Cairo", "Nairobi", "Masai Mara", "Zanzibar", "Cape Town",
    "Sydney", "Melbourne", "Gold Coast", "Auckland", "Queenstown",
    "Hawaii", "New York", "Las Vegas", "Orlando", "Cancun",
    # India
    "Andaman", "Andaman and Nicobar", "Lakshadweep", "Kashmir", "Ladakh",
    "Leh", "Himachal", "Himachal Pradesh", "Shimla", "Manali",
    "Uttarakhand", "Rishikesh", "Rajasthan", "Jaipur", "Udaipur",
    "Jaisalmer", "Goa", "Kerala", "Munnar", "Sikkim", "Gangtok",
    "Darjeeling", "Meghalaya", "Shillong", "Assam", "Arunachal",
    "North East India", "Northeast India", "Gujarat", "Kutch",
    "Varanasi", "Agra", "Ooty", "Coorg", "Pondicherry", "Puducherry",
    "Tamil Nadu", "Karnataka", "Odisha", "Madhya Pradesh",
]

# Words that may appear between destination names without breaking a list
FILLER_WORDS = {"and", "the", "of", "to", "in", "more", "etc"}


def _key(text):
    """Lower-case, letters only - used for matching."""
    return re.sub(r"[^a-z]", "", text.lower())


# Build lookup tables
_ALIASES = {}          # key -> canonical name
for name, aliases in COUNTRIES.items():
    for spelling in [name, *aliases]:
        _ALIASES[_key(spelling)] = name
for place in PLACES:
    _ALIASES.setdefault(_key(place), place)

# Multi-word names, longest first, as tuples of word keys
_MULTI = sorted(
    {
        tuple(_key(w) for w in spelling.replace("-", " ").split())
        for spelling in [
            *COUNTRIES, *(a for al in COUNTRIES.values() for a in al), *PLACES
        ]
        if len(spelling.replace("-", " ").split()) > 1
    },
    key=len,
    reverse=True,
)
# Single-word keys long enough to fuzzy-match safely
_FUZZY_KEYS = [k for k in _ALIASES if len(k) >= 6]
# Keys used to split joined words; short ones (Goa, Leh, Oman...) are
# excluded so they are not found inside unrelated words.
_SPLIT_KEYS = sorted((k for k in _ALIASES if len(k) >= 5), key=len, reverse=True)


def canonical(name):
    """Return the standard spelling for a place name, or None."""
    return _ALIASES.get(_key(name))


def _split_joined(word_key):
    """Split 'armeniauzbekistan' into ['Armenia', 'Uzbekistan'] if possible."""
    if not word_key:
        return []
    for key in _SPLIT_KEYS:
        if word_key.startswith(key):
            rest = _split_joined(word_key[len(key):]) if word_key != key else []
            if word_key == key or rest:
                return [_ALIASES[key], *rest]
    return None


def _fuzzy(word_key):
    if len(word_key) < 6:
        return None
    match = difflib.get_close_matches(word_key, _FUZZY_KEYS, n=1, cutoff=0.85)
    return _ALIASES[match[0]] if match else None


def places_in_line(line):
    """
    Return (places, coverage) for one OCR line.
    coverage = share of the line's letters that belong to place names.
    """
    # Separate camel-case joins: "ArmeniaUzbekistan" -> "Armenia Uzbekistan"
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", line)
    words = re.findall(r"[A-Za-zÀ-ÿ]+", spaced)
    keys = [_key(w) for w in words]

    total_letters = sum(len(k) for k in keys if k not in FILLER_WORDS)
    if not total_letters:
        return [], 0.0

    found, covered, i = [], 0, 0
    while i < len(keys):
        if not keys[i]:
            i += 1
            continue

        # 1) multi-word names ("Sri Lanka", "Abu Dhabi")
        matched = False
        for combo in _MULTI:
            n = len(combo)
            if tuple(keys[i:i + n]) == combo:
                found.append(_ALIASES.get("".join(combo)) or
                             " ".join(w.capitalize() for w in words[i:i + n]))
                covered += sum(len(k) for k in combo)
                i += n
                matched = True
                break
        if matched:
            continue

        key = keys[i]
        # 2) exact single word
        if key in _ALIASES:
            found.append(_ALIASES[key])
            covered += len(key)
        # 3) several names joined together
        elif (parts := _split_joined(key)):
            found.extend(parts)
            covered += len(key)
        # 4) small OCR spelling mistakes
        elif (fixed := _fuzzy(key)):
            found.append(fixed)
            covered += len(key)
        i += 1

    return found, covered / total_letters


def find_destination_list(ocr_text):
    """
    Find destinations that the card lists explicitly, e.g. a strip
    'Kazakhstan • Azerbaijan • Belarus' or one destination per line.
    Address lines, office labels and single mentions are ignored.
    """
    result = []

    def add(names):
        for name in names:
            if name not in result:
                result.append(name)

    lines = [l.strip() for l in ocr_text.splitlines() if l.strip()]
    run = []   # consecutive lines that are just one place name each

    for line in lines:
        places, coverage = places_in_line(line)

        if len(places) >= 2 and coverage >= 0.6:
            add(places)            # a destination strip on one line
            run = []
        elif len(places) == 1 and coverage >= 0.85:
            run.append(places[0])  # maybe part of a vertical list
        else:
            if len(run) >= 3:
                add(run)
            run = []

    if len(run) >= 3:
        add(run)

    return result


def mentioned_in_text(name, ocr_text):
    """True if a destination name (or a close OCR misspelling) is in the text."""
    target = _key(name)
    if not target:
        return False
    text_key = _key(ocr_text)
    if target in text_key:
        return True
    canon = canonical(name)
    if canon:
        for line in ocr_text.splitlines():
            if canon in places_in_line(line)[0]:
                return True
    return False
