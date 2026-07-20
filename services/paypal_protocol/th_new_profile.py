"""Country-specific data and request adaptation for the TH New worker."""
from __future__ import annotations

import copy
import random
import re
import string
from typing import Any, TypeVar


_T = TypeVar("_T")

TH_FIRST_NAMES = (
    "Anan", "Arthit", "Chai", "Kiet", "Narin", "Preecha", "Sakda",
    "Somchai", "Thanawat", "Wichai", "Achara", "Busaba", "Kanya",
    "Lalita", "Malee", "Nisa", "Pimchan", "Siriporn", "Suda", "Wipa",
)

TH_LAST_NAMES = (
    "Srisai", "Saelim", "Thongchai", "Kittisak", "Rattanakul", "Boonmee",
    "Sukjai", "Wongsawat", "Chantarangsu", "Phromdee", "Kaewla", "Maneerat",
    "Prasert", "Suwan", "Intarapong", "Chaiyasit", "Raksakul", "Somboon",
)

# Public commercial address shapes avoid using private residential addresses.
TH_ADDRESSES = (
    ("Rama I Road", "999", "Pathum Wan", "Bangkok", "Bangkok", "10330"),
    ("Ratchadaphisek Road", "9", "Chatuchak", "Bangkok", "Bangkok", "10900"),
    ("Sukhumvit Road", "88", "Khlong Toei", "Bangkok", "Bangkok", "10110"),
    ("Si Lom Road", "191", "Bang Rak", "Bangkok", "Bangkok", "10500"),
)


def apply_th_browser_profile(profile: dict[str, Any]) -> dict[str, Any]:
    profile.update({
        "country": "TH",
        "language": "th-TH",
        "locale": "th_TH",
        "accept_language": "th-TH,th;q=0.9,en;q=0.8",
        "timezone": "Asia/Bangkok",
        # JavaScript Date#getTimezoneOffset for UTC+7 is -420 minutes.
        "timezone_offset_minutes": -420,
        "timezone_offset_ms": -420 * 60 * 1000,
        "dst": False,
    })
    return profile


def normalize_th_phone(phone: str) -> tuple[str, str, str]:
    raw = str(phone or "").strip()
    if raw.casefold().startswith("phone:"):
        raw = raw.split(":", 1)[1].strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("66") and len(digits) == 11:
        local = digits[2:]
    else:
        local = digits
        if local.startswith("0") and len(local) == 10:
            local = local[1:]
    if len(local) != 9:
        raise ValueError("TH local phone number must contain 9 digits")
    return f"+66{local}", "+66", local


def _generate_password() -> str:
    values = [
        *(random.choice(string.ascii_lowercase) for _ in range(6)),
        *(random.choice(string.ascii_uppercase) for _ in range(3)),
        *(random.choice(string.digits) for _ in range(3)),
        *(random.choice("!@#$%^") for _ in range(2)),
    ]
    random.shuffle(values)
    return "".join(values)


def generate_th_national_id() -> str:
    digits = [random.randint(1, 8), *(random.randint(0, 9) for _ in range(11))]
    checksum = (11 - sum(value * weight for value, weight in zip(digits, range(13, 1, -1))) % 11) % 10
    return "".join(str(value) for value in (*digits, checksum))


def generate_th_user(user_type: type[_T], phone: str) -> _T:
    full_phone, country_code, local = normalize_th_phone(phone)
    email_user = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(12))
    return user_type(
        first_name=random.choice(TH_FIRST_NAMES),
        last_name=random.choice(TH_LAST_NAMES),
        email=f"{email_user}@gmail.com",
        phone=full_phone,
        phone_local=local,
        phone_country_code=country_code,
        password=_generate_password(),
        dob=f"{random.randint(1, 28):02d}/{random.randint(1, 12):02d}/{random.randint(1980, 2000)}",
        cpf=generate_th_national_id(),
    )


def generate_th_address(address_type: type[_T]) -> _T:
    street, house_number, district, city, state, postal_code = random.choice(TH_ADDRESSES)
    return address_type(
        street=street,
        house_number=house_number,
        district=district,
        city=city,
        state=state,
        postal_code=postal_code,
        country="TH",
    )


def adapt_th_graphql_variables(operation_name: str, variables: dict[str, Any]) -> dict[str, Any]:
    adapted = copy.deepcopy(variables)
    if operation_name == "InitiateRiskBasedTwoFactorPhoneConfirmationMutation":
        adapted["phoneCountry"] = "TH"
        adapted["locale"] = {"country": "TH", "lang": "th"}
    elif operation_name == "GriffinMetadataQuery":
        adapted["countryCode"] = "TH"
        adapted["shippingCountryCode"] = "TH"
        adapted["languageCode"] = "th"
    return adapted


def adapt_th_signup_variables(
    variables: dict[str, Any],
    *,
    first_name: str,
    last_name: str,
) -> dict[str, Any]:
    adapted = copy.deepcopy(variables)
    adapted["country"] = "TH"
    card = adapted.get("card")
    if isinstance(card, dict):
        card.pop("productClass", None)

    billing = adapted.get("billingAddress")
    if not isinstance(billing, dict):
        billing = {}
        adapted["billingAddress"] = billing
    billing["country"] = "TH"
    billing.setdefault("accountQuality", {})
    billing["accountQuality"].update({
        "autoCompleteType": "GOOGLE",
        "isUserModified": False,
    })
    billing.pop("line2", None)

    shipping = adapted.get("shippingAddress")
    if isinstance(shipping, dict):
        shipping["country"] = "TH"

    identity = adapted.get("identityDocument")
    national_id = str(identity.get("value") or "") if isinstance(identity, dict) else ""
    adapted["marketingOptOut"] = False
    adapted["identityDocument"] = {
        "type": "NATIONAL_ID",
        "value": national_id,
    }
    adapted["crsData"] = None
    adapted["nationality"] = "TH"
    adapted.pop("residentialAddress", None)
    return adapted


def th_billing_line1(street: str, house_number: str) -> str:
    street = re.sub(r"\s+", " ", str(street or "")).strip()
    house_number = re.sub(r"\s+", " ", str(house_number or "")).strip()
    return f"{house_number} {street}".strip()
