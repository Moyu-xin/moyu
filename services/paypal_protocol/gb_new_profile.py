"""Country-specific data and request adaptation for the GB New worker."""
from __future__ import annotations

import copy
import random
import re
import string
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar


_T = TypeVar("_T")

GB_FIRST_NAMES = (
    "Oliver", "George", "Harry", "Jack", "Jacob", "Noah", "Charlie",
    "Thomas", "Oscar", "William", "Amelia", "Olivia", "Emily", "Isla",
    "Ava", "Jessica", "Poppy", "Ella", "Lily", "Sophie",
)

GB_LAST_NAMES = (
    "Smith", "Jones", "Taylor", "Brown", "Williams", "Wilson", "Johnson",
    "Davies", "Patel", "Robinson", "Wright", "Thompson", "Evans", "Walker",
    "White", "Roberts", "Green", "Hall", "Thomas", "Clarke",
)

# Publicly recognizable commercial addresses keep the generated format valid
# without using a private residential address.
GB_ADDRESSES = (
    ("Canada Square", "1", "", "London", "Greater London", "E14 5AB"),
    ("New Street Square", "1", "", "London", "Greater London", "EC4A 3HQ"),
    ("Centenary Square", "1", "", "Birmingham", "West Midlands", "B1 2EA"),
    ("St Andrew Square", "6", "", "Edinburgh", "Scotland", "EH2 2BD"),
)


def _last_sunday(year: int, month: int) -> datetime:
    if month == 12:
        next_month = datetime(year + 1, 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(year, month + 1, 1, 1, tzinfo=timezone.utc)
    last_day = next_month - timedelta(days=1)
    return last_day - timedelta(days=(last_day.weekday() - 6) % 7)


def gb_browser_timezone(now: datetime | None = None) -> tuple[int, bool]:
    """Return JavaScript getTimezoneOffset minutes and UK DST state."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    dst = _last_sunday(current.year, 3) <= current < _last_sunday(current.year, 10)
    return (-60 if dst else 0), dst


def apply_gb_browser_profile(profile: dict[str, Any]) -> dict[str, Any]:
    offset, dst = gb_browser_timezone()
    profile.update({
        "country": "GB",
        "language": "en-GB",
        "locale": "en_GB",
        "accept_language": "en-GB,en;q=0.9",
        "timezone": "Europe/London",
        "timezone_offset_minutes": offset,
        "timezone_offset_ms": offset * 60 * 1000,
        "dst": dst,
    })
    return profile


def normalize_gb_phone(phone: str) -> tuple[str, str, str]:
    raw = str(phone or "").strip()
    if raw.casefold().startswith("phone:"):
        raw = raw.split(":", 1)[1].strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("44") and 9 <= len(digits[2:]) <= 10:
        local = digits[2:]
    else:
        local = digits
        if local.startswith("0") and len(local) == 11:
            local = local[1:]
    if not 9 <= len(local) <= 10:
        raise ValueError("GB local phone number must contain 9-10 digits")
    return f"+44{local}", "+44", local


def _generate_password() -> str:
    values = [
        *(random.choice(string.ascii_lowercase) for _ in range(6)),
        *(random.choice(string.ascii_uppercase) for _ in range(3)),
        *(random.choice(string.digits) for _ in range(3)),
        *(random.choice("!@#$%^") for _ in range(2)),
    ]
    random.shuffle(values)
    return "".join(values)


def generate_gb_user(user_type: type[_T], phone: str) -> _T:
    full_phone, country_code, local = normalize_gb_phone(phone)
    email_user = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(12))
    return user_type(
        first_name=random.choice(GB_FIRST_NAMES),
        last_name=random.choice(GB_LAST_NAMES),
        email=f"{email_user}@gmail.com",
        phone=full_phone,
        phone_local=local,
        phone_country_code=country_code,
        password=_generate_password(),
        dob=f"{random.randint(1, 28):02d}/{random.randint(1, 12):02d}/{random.randint(1980, 2000)}",
        cpf="",
    )


def generate_gb_address(address_type: type[_T]) -> _T:
    street, house_number, district, city, state, postal_code = random.choice(GB_ADDRESSES)
    return address_type(
        street=street,
        house_number=house_number,
        district=district,
        city=city,
        state=state,
        postal_code=postal_code,
        country="GB",
    )


def adapt_gb_graphql_variables(operation_name: str, variables: dict[str, Any]) -> dict[str, Any]:
    adapted = copy.deepcopy(variables)
    if operation_name == "InitiateRiskBasedTwoFactorPhoneConfirmationMutation":
        adapted["phoneCountry"] = "GB"
        adapted["locale"] = {"country": "GB", "lang": "en"}
    elif operation_name == "GriffinMetadataQuery":
        adapted["countryCode"] = "GB"
        adapted["shippingCountryCode"] = "GB"
        adapted["languageCode"] = "en"
    return adapted


def adapt_gb_signup_variables(
    variables: dict[str, Any],
    *,
    first_name: str,
    last_name: str,
) -> dict[str, Any]:
    adapted = copy.deepcopy(variables)
    adapted["country"] = "GB"
    card = adapted.get("card")
    if isinstance(card, dict):
        card.pop("productClass", None)

    billing = adapted.get("billingAddress")
    if not isinstance(billing, dict):
        billing = {}
        adapted["billingAddress"] = billing
    billing["country"] = "GB"
    billing.setdefault("accountQuality", {})
    billing["accountQuality"].update({
        "autoCompleteType": "MANUAL",
        "isUserModified": True,
    })
    if not str(billing.get("line2") or "").strip():
        billing.pop("line2", None)

    shipping = adapted.get("shippingAddress")
    if isinstance(shipping, dict):
        shipping["country"] = "GB"
        shipping.pop("state", None)

    adapted["marketingOptOut"] = False
    adapted.pop("identityDocument", None)
    adapted["crsData"] = {
        "firstName": first_name,
        "lastName": last_name,
        "subjectToTaxOutsideLegalCountry": False,
        "taxDetails": [{"countryCode": "GB"}],
    }
    adapted["nationality"] = "GB"
    adapted["residentialAddress"] = copy.deepcopy(billing)
    return adapted


def gb_billing_line1(street: str, house_number: str) -> str:
    street = re.sub(r"\s+", " ", str(street or "")).strip()
    house_number = re.sub(r"\s+", " ", str(house_number or "")).strip()
    return f"{house_number} {street}".strip()
