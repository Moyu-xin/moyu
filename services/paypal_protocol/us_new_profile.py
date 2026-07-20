"""Country-specific data and request adaptation for the US New worker."""
from __future__ import annotations

import copy
import random
import re
import string
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar


_T = TypeVar("_T")

US_FIRST_NAMES = (
    "James", "Michael", "William", "David", "Joseph", "Daniel", "Matthew",
    "Christopher", "Andrew", "Joshua", "Emma", "Olivia", "Sophia", "Ava",
    "Isabella", "Mia", "Charlotte", "Amelia", "Harper", "Evelyn",
)

US_LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
)

# Public commercial addresses provide valid US address shapes without using
# private residential addresses.
US_ADDRESSES = (
    ("Fifth Avenue", "350", "", "New York", "NY", "10118"),
    ("Broadway", "1501", "", "New York", "NY", "10036"),
    ("Madison Avenue", "383", "", "New York", "NY", "10179"),
    ("Liberty Street", "28", "", "New York", "NY", "10005"),
)


def us_browser_timezone(now: datetime | None = None) -> tuple[int, bool]:
    """Return JavaScript getTimezoneOffset minutes and New York DST state."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)

    march_first = datetime(current.year, 3, 1, tzinfo=timezone.utc)
    first_sunday = march_first + timedelta(days=(6 - march_first.weekday()) % 7)
    second_sunday = first_sunday + timedelta(days=7)
    # DST starts at 02:00 EST, which is 07:00 UTC.
    dst_start = second_sunday.replace(hour=7)

    november_first = datetime(current.year, 11, 1, tzinfo=timezone.utc)
    end_sunday = november_first + timedelta(days=(6 - november_first.weekday()) % 7)
    # DST ends at 02:00 EDT, which is 06:00 UTC.
    dst_end = end_sunday.replace(hour=6)
    dst = dst_start <= current < dst_end
    return (240 if dst else 300), dst


def apply_us_browser_profile(profile: dict[str, Any]) -> dict[str, Any]:
    offset, dst = us_browser_timezone()
    profile.update({
        "country": "US",
        "language": "en-US",
        "locale": "en_US",
        "accept_language": "en-US,en;q=0.9",
        "timezone": "America/New_York",
        "timezone_offset_minutes": offset,
        "timezone_offset_ms": offset * 60 * 1000,
        "dst": dst,
    })
    return profile


def normalize_us_phone(phone: str) -> tuple[str, str, str]:
    raw = str(phone or "").strip()
    if raw.casefold().startswith("phone:"):
        raw = raw.split(":", 1)[1].strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("1"):
        local = digits[1:]
    else:
        local = digits
    if len(local) != 10:
        raise ValueError("US local phone number must contain 10 digits")
    return f"+1{local}", "+1", local


def _generate_password() -> str:
    values = [
        *(random.choice(string.ascii_lowercase) for _ in range(6)),
        *(random.choice(string.ascii_uppercase) for _ in range(3)),
        *(random.choice(string.digits) for _ in range(3)),
        *(random.choice("!@#$%^") for _ in range(2)),
    ]
    random.shuffle(values)
    return "".join(values)


def generate_us_user(user_type: type[_T], phone: str) -> _T:
    full_phone, country_code, local = normalize_us_phone(phone)
    email_user = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(12))
    return user_type(
        first_name=random.choice(US_FIRST_NAMES),
        last_name=random.choice(US_LAST_NAMES),
        email=f"{email_user}@gmail.com",
        phone=full_phone,
        phone_local=local,
        phone_country_code=country_code,
        password=_generate_password(),
        dob=f"{random.randint(1, 28):02d}/{random.randint(1, 12):02d}/{random.randint(1980, 2000)}",
        cpf="",
    )


def generate_us_address(address_type: type[_T]) -> _T:
    street, house_number, district, city, state, postal_code = random.choice(US_ADDRESSES)
    return address_type(
        street=street,
        house_number=house_number,
        district=district,
        city=city,
        state=state,
        postal_code=postal_code,
        country="US",
    )


def adapt_us_graphql_variables(operation_name: str, variables: dict[str, Any]) -> dict[str, Any]:
    adapted = copy.deepcopy(variables)
    if operation_name == "InitiateRiskBasedTwoFactorPhoneConfirmationMutation":
        adapted["phoneCountry"] = "US"
        adapted["locale"] = {"country": "US", "lang": "en"}
    elif operation_name == "GriffinMetadataQuery":
        adapted["countryCode"] = "US"
        adapted["shippingCountryCode"] = "US"
        adapted["languageCode"] = "en"
    return adapted


def adapt_us_signup_variables(variables: dict[str, Any]) -> dict[str, Any]:
    adapted = copy.deepcopy(variables)
    adapted["country"] = "US"
    card = adapted.get("card")
    if isinstance(card, dict):
        card.pop("productClass", None)

    billing = adapted.get("billingAddress")
    if not isinstance(billing, dict):
        billing = {}
        adapted["billingAddress"] = billing
    billing["country"] = "US"
    billing.setdefault("accountQuality", {})
    billing["accountQuality"].update({
        "autoCompleteType": "GOOGLE",
        "isUserModified": False,
    })
    if not str(billing.get("line2") or "").strip():
        billing.pop("line2", None)

    shipping = adapted.get("shippingAddress")
    if isinstance(shipping, dict):
        shipping["country"] = "US"

    adapted["marketingOptOut"] = False
    adapted.pop("identityDocument", None)
    adapted.pop("dateOfBirth", None)
    adapted.pop("nationality", None)
    adapted.pop("residentialAddress", None)
    adapted["crsData"] = None
    return adapted


def us_billing_line1(street: str, house_number: str) -> str:
    street = re.sub(r"\s+", " ", str(street or "")).strip()
    house_number = re.sub(r"\s+", " ", str(house_number or "")).strip()
    return f"{house_number} {street}".strip()
