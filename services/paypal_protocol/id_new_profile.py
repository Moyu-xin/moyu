"""Country-specific data and request adaptation for the ID New worker."""
from __future__ import annotations

import copy
import random
import re
import string
from typing import Any, TypeVar


_T = TypeVar("_T")

ID_FIRST_NAMES = (
    "Adi", "Agus", "Andi", "Arif", "Budi", "Dimas", "Eko", "Fajar",
    "Hendra", "Rizky", "Ayu", "Dewi", "Fitri", "Indah", "Kartika",
    "Lestari", "Maya", "Nadia", "Putri", "Sari",
)

ID_LAST_NAMES = (
    "Santoso", "Setiawan", "Wijaya", "Saputra", "Pratama", "Kurniawan",
    "Hidayat", "Nugroho", "Siregar", "Permana", "Wibowo", "Susanto",
    "Gunawan", "Hartono", "Ramadhan", "Maulana", "Utama", "Firmansyah",
)

# Public commercial address shapes avoid using private residential addresses.
ID_ADDRESSES = (
    ("Jalan M.H. Thamrin", "1", "Menteng", "Jakarta Pusat", "DKI Jakarta", "10310"),
    ("Jalan Jenderal Sudirman", "52-53", "Kebayoran Baru", "Jakarta Selatan", "DKI Jakarta", "12190"),
    ("Jalan Asia Afrika", "8", "Gelora", "Jakarta Pusat", "DKI Jakarta", "10270"),
    ("Jalan Pemuda", "150", "Sekayu", "Semarang Tengah", "Jawa Tengah", "50132"),
)


def apply_id_browser_profile(profile: dict[str, Any]) -> dict[str, Any]:
    profile.update({
        "country": "ID",
        "language": "id-ID",
        "locale": "id_ID",
        "accept_language": "id-ID,id;q=0.9,en;q=0.8",
        "timezone": "Asia/Jakarta",
        # JavaScript Date#getTimezoneOffset for UTC+7 is -420 minutes.
        "timezone_offset_minutes": -420,
        "timezone_offset_ms": -420 * 60 * 1000,
        "dst": False,
    })
    return profile


def normalize_id_phone(phone: str) -> tuple[str, str, str]:
    raw = str(phone or "").strip()
    if raw.casefold().startswith("phone:"):
        raw = raw.split(":", 1)[1].strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    international = raw.startswith("+") or digits.startswith("00")
    if digits.startswith("00"):
        digits = digits[2:]
    has_calling_code = digits.startswith("62") and 9 <= len(digits[2:]) <= 12
    if international and not has_calling_code:
        raise ValueError("phone number does not match ID calling code +62")
    if has_calling_code:
        local = digits[2:]
    else:
        local = digits
        if local.startswith("0") and 9 <= len(local[1:]) <= 12:
            local = local[1:]
    if not 9 <= len(local) <= 12:
        raise ValueError("ID local phone number must contain 9-12 digits")
    return f"+62{local}", "+62", local


def _generate_password() -> str:
    values = [
        *(random.choice(string.ascii_lowercase) for _ in range(6)),
        *(random.choice(string.ascii_uppercase) for _ in range(3)),
        *(random.choice(string.digits) for _ in range(3)),
        *(random.choice("!@#$%^") for _ in range(2)),
    ]
    random.shuffle(values)
    return "".join(values)


def generate_id_user(user_type: type[_T], phone: str) -> _T:
    full_phone, country_code, local = normalize_id_phone(phone)
    email_user = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(12))
    return user_type(
        first_name=random.choice(ID_FIRST_NAMES),
        last_name=random.choice(ID_LAST_NAMES),
        email=f"{email_user}@gmail.com",
        phone=full_phone,
        phone_local=local,
        phone_country_code=country_code,
        password=_generate_password(),
        dob=f"{random.randint(1, 28):02d}/{random.randint(1, 12):02d}/{random.randint(1980, 2000)}",
        cpf="",
    )


def generate_id_address(address_type: type[_T]) -> _T:
    street, house_number, district, city, state, postal_code = random.choice(ID_ADDRESSES)
    return address_type(
        street=street,
        house_number=house_number,
        district=district,
        city=city,
        state=state,
        postal_code=postal_code,
        country="ID",
    )


def adapt_id_graphql_variables(operation_name: str, variables: dict[str, Any]) -> dict[str, Any]:
    adapted = copy.deepcopy(variables)
    if operation_name == "InitiateRiskBasedTwoFactorPhoneConfirmationMutation":
        adapted["phoneCountry"] = "ID"
        adapted["locale"] = {"country": "ID", "lang": "id"}
    elif operation_name == "GriffinMetadataQuery":
        adapted["countryCode"] = "ID"
        adapted["shippingCountryCode"] = "ID"
        adapted["languageCode"] = "id"
    return adapted


def adapt_id_signup_variables(
    variables: dict[str, Any],
    *,
    first_name: str,
    last_name: str,
) -> dict[str, Any]:
    adapted = copy.deepcopy(variables)
    adapted["country"] = "ID"
    card = adapted.get("card")
    if isinstance(card, dict):
        card.pop("productClass", None)

    billing = adapted.get("billingAddress")
    if not isinstance(billing, dict):
        billing = {}
        adapted["billingAddress"] = billing
    billing["country"] = "ID"
    billing.setdefault("accountQuality", {})
    billing["accountQuality"].update({
        "autoCompleteType": "MANUAL",
        "isUserModified": True,
    })
    if not str(billing.get("line2") or "").strip():
        billing.pop("line2", None)

    shipping = adapted.get("shippingAddress")
    if isinstance(shipping, dict):
        shipping["country"] = "ID"

    adapted["marketingOptOut"] = False
    adapted.pop("identityDocument", None)
    adapted["crsData"] = {
        "firstName": first_name,
        "lastName": last_name,
        "subjectToTaxOutsideLegalCountry": False,
        "taxDetails": [{"countryCode": "ID"}],
    }
    adapted["nationality"] = "ID"
    adapted["residentialAddress"] = copy.deepcopy(billing)
    return adapted


def id_billing_line1(street: str, house_number: str) -> str:
    street = re.sub(r"\s+", " ", str(street or "")).strip()
    house_number = re.sub(r"\s+", " ", str(house_number or "")).strip()
    return f"{street} No. {house_number}".strip()
