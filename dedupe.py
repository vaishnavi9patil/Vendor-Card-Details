"""
Duplicate detection and merging for imports.

Two records are treated as the same vendor when:
  1. they share an email address (and the contact names don't clearly differ),
  2. they have the same company (ignoring "Pvt Ltd", "LLP", dots, case ...)
     and the same contact - short and full forms of a name count as the same
     ("Suvit Rokade" = "Suvit Vijay Rokade") - or one/both have no contact,
  3. they share a phone number and are the same person (or one has no name).
"""

import re

COMPANY_NOISE = {
    "pvt", "private", "ltd", "limited", "llp", "llc", "inc", "incorporated",
    "pte", "plc", "co", "corp", "corporation", "the", "and", "sdn", "bhd",
    "gmbh", "fze", "fzco", "fz", "opc", "lp", "company",
}
NAME_TITLES = {"mr", "mrs", "ms", "miss", "dr", "shri", "smt", "sri", "prof"}


def norm_company(name):
    """'Kazin DMC Pvt. Ltd.' -> 'kazindmc'"""
    words = re.findall(r"[a-z0-9]+", (name or "").lower().replace("&", " and "))
    words = [w for w in words if w not in COMPANY_NOISE]
    return "".join(words)


def name_tokens(name):
    """'Mr. Suvit V. Rokade' -> ['suvit', 'v', 'rokade']"""
    words = re.findall(r"[a-z]+", (name or "").lower())
    return [w for w in words if w not in NAME_TITLES]


def same_person(a, b):
    """True when two names are the same person, allowing short forms."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    long_, short = (ta, tb) if len(ta) >= len(tb) else (tb, ta)
    # Initials: "S. Rokade" vs "Suvit Rokade"
    def covers(word, other):
        return word == other or (len(word) == 1 and other.startswith(word)) or \
            (len(other) == 1 and word.startswith(other))
    # First and last name agree (middle names may be missing)
    if covers(short[0], long_[0]) and covers(short[-1], long_[-1]) and len(short) >= 2:
        return True
    # All words of the shorter name appear in the longer one
    if len(short) >= 2 and all(any(covers(s, l) for l in long_) for s in short):
        return True
    return False


def same_person_loose(a, b):
    """Like same_person, but a lone first name also matches
    ("Suvit" = "Suvit Rokade"). Only used within the same company."""
    if same_person(a, b):
        return True
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return False
    short, long_ = (ta, tb) if len(ta) < len(tb) else (tb, ta)
    return len(short) == 1 and short[0] in (long_[0], long_[-1])


def names_conflict(a, b):
    """Both names present and clearly different people."""
    return bool(name_tokens(a)) and bool(name_tokens(b)) and not same_person(a, b)


def phone_keys(phones):
    """Last 10 digits of each number (ignores +91 / 0 prefixes, spaces)."""
    keys = set()
    for phone in phones or []:
        # "+91 124-4200130/31" -> use the part before "/" extensions
        digits = re.sub(r"\D", "", phone.split("/")[0])
        if len(digits) >= 8:
            keys.add(digits[-10:])
    return keys


def email_keys(emails):
    return {e.strip().lower() for e in emails or [] if "@" in e}


# ------------------------------------------------------------------ merge

LIST_FIELDS = ("phone_numbers", "emails", "destinations_covered", "websites",
               "other_contacts")
TEXT_FIELDS = ("company", "person_name", "designation", "address", "services_notes")


def _combine(old, new, phone=False):
    """Old list + new items that are not already there."""
    result = list(old or [])
    seen_keys = set()
    for item in result:
        seen_keys |= phone_keys([item]) if phone else {item.strip().lower()}
    for item in new or []:
        key = phone_keys([item]) if phone else {item.strip().lower()}
        if key and key & seen_keys:
            continue
        if not key and item in result:
            continue
        result.append(item)
        seen_keys |= key
    return result


def merge_records(old, new, mode="update"):
    """
    Combine two versions of a vendor.
      mode="update": file values replace saved text values (unless the file
                     value is just a shorter part of the saved one)
      mode="fill":   saved values are kept; only empty fields are filled
    The saved company name is always kept.
    Phones, emails, websites, destinations and other contacts are always
    combined, so no number or email is lost.
    """
    merged = {}
    also_known = ""
    for field in TEXT_FIELDS:
        old_value, new_value = (old.get(field) or "").strip(), (new.get(field) or "").strip()
        if field == "company":
            # Never rename a saved vendor's company. If the file uses a
            # different company name, remember it in the notes instead.
            merged[field] = old_value or new_value
            if old_value and new_value and norm_company(old_value) != norm_company(new_value):
                also_known = f"Also listed as: {new_value}"
            continue
        if old_value and new_value and new_value.lower() in old_value.lower():
            # The file has less detail (e.g. "Gurugram" vs the full address)
            merged[field] = old_value
            continue
        if field == "person_name" and old_value and new_value and same_person(old_value, new_value):
            # Same person written differently - keep the fuller form
            merged[field] = new_value if len(name_tokens(new_value)) > len(name_tokens(old_value)) else old_value
            continue
        if field == "services_notes" and old_value and new_value:
            if new_value.lower() in old_value.lower():
                merged[field] = old_value
            elif old_value.lower() in new_value.lower():
                merged[field] = new_value
            else:
                merged[field] = f"{old_value}; {new_value}"
            continue
        if mode == "fill":
            merged[field] = old_value or new_value
        else:
            merged[field] = new_value or old_value
    if also_known and also_known.lower() not in merged["services_notes"].lower():
        merged["services_notes"] = "; ".join(p for p in (merged["services_notes"], also_known) if p)
    for field in LIST_FIELDS:
        merged[field] = _combine(old.get(field), new.get(field), phone=(field == "phone_numbers"))
    return merged


# ---------------------------------------------------------------- matcher

class Matcher:
    """Index of known vendors that finds the record a new row matches."""

    def __init__(self):
        self.entries = []
        self.by_email, self.by_phone, self.by_company = {}, {}, {}

    def add(self, vendor, ref):
        entry = {"ref": ref, "vendor": dict(vendor)}
        self.entries.append(entry)
        self._index(entry)
        return entry

    def _index(self, entry):
        v = entry["vendor"]
        for e in email_keys(v.get("emails")):
            self.by_email.setdefault(e, []).append(entry)
        for p in phone_keys(v.get("phone_numbers")):
            self.by_phone.setdefault(p, []).append(entry)
        company = norm_company(v.get("company"))
        if company:
            self.by_company.setdefault(company, []).append(entry)

    def update(self, entry, vendor):
        """After a merge the record knows more phones/emails - re-index."""
        entry["vendor"] = dict(vendor)
        self._index(entry)

    def find(self, vendor):
        """Return (entry, reason) for the best match, or (None, None)."""
        person = vendor.get("person_name") or ""
        company = norm_company(vendor.get("company"))

        def candidates(index, keys):
            seen = []
            for key in keys:
                for entry in index.get(key, []):
                    if entry not in seen:
                        seen.append(entry)
            return seen

        # 1. Same email
        for entry in candidates(self.by_email, email_keys(vendor.get("emails"))):
            if not names_conflict(person, entry["vendor"].get("person_name")):
                return entry, "same email"

        # 2. Same company and same contact (or no contact on one side)
        if company:
            for entry in self.by_company.get(company, []):
                other = entry["vendor"].get("person_name") or ""
                if same_person_loose(person, other):
                    return entry, "same company and contact"
            for entry in self.by_company.get(company, []):
                other = entry["vendor"].get("person_name") or ""
                if not name_tokens(person) or not name_tokens(other):
                    return entry, "same company"

        # 3. Same phone number and the same person
        for entry in candidates(self.by_phone, phone_keys(vendor.get("phone_numbers"))):
            other = entry["vendor"]
            other_company = norm_company(other.get("company"))
            if same_person(person, other.get("person_name")):
                return entry, "same phone and contact"
            if not name_tokens(person) and not name_tokens(other.get("person_name")) \
                    and (not company or not other_company or company == other_company):
                return entry, "same phone"

        return None, None
