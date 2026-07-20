from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class CountryProfile:
    code: str
    name: str
    locale: str
    language: str
    accept_language: str
    currency: str
    phone_calling_code: str
    phone_local_min_length: int
    phone_local_max_length: int
    phone_example: str
    trunk_prefix: str
    timezone: str
    standard_utc_offset_minutes: int
    dst_utc_offset_minutes: int
    content_hash_fallback: str
    identity_document_type: str | None
    include_crs_data: bool
    include_residential_address: bool
    marketing_opt_out: bool
    billing_autocomplete_type: str
    billing_is_user_modified: bool
    include_card_product_class: bool
    include_date_of_birth: bool
    include_shipping_state: bool
    supports_postal_normalization: bool
    house_number_first: bool

    @property
    def browser_language(self) -> str:
        return self.locale.replace("_", "-")

    def utc_offset_minutes(self, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)

        if self.code == "GB":
            dst_start = _last_sunday(current.year, 3, hour=1)
            dst_end = _last_sunday(current.year, 10, hour=1)
            if dst_start <= current < dst_end:
                return self.dst_utc_offset_minutes
        elif self.code == "US":
            dst_start = _nth_sunday(current.year, 3, 2, hour=7)
            dst_end = _nth_sunday(current.year, 11, 1, hour=6)
            if dst_start <= current < dst_end:
                return self.dst_utc_offset_minutes

        return self.standard_utc_offset_minutes


def _nth_sunday(year: int, month: int, occurrence: int, *, hour: int) -> datetime:
    first = datetime(year, month, 1, hour, tzinfo=timezone.utc)
    days_until_sunday = (6 - first.weekday()) % 7
    return first + timedelta(days=days_until_sunday + (occurrence - 1) * 7)


def _last_sunday(year: int, month: int, *, hour: int) -> datetime:
    if month == 12:
        next_month = datetime(year + 1, 1, 1, hour, tzinfo=timezone.utc)
    else:
        next_month = datetime(year, month + 1, 1, hour, tzinfo=timezone.utc)
    last_day = next_month - timedelta(days=1)
    return last_day - timedelta(days=(last_day.weekday() - 6) % 7)


COUNTRY_PROFILES: dict[str, CountryProfile] = {
    "BR": CountryProfile(
        code="BR",
        name="Brazil",
        locale="pt_BR",
        language="pt",
        accept_language="pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        currency="BRL",
        phone_calling_code="55",
        phone_local_min_length=10,
        phone_local_max_length=11,
        phone_example="+5591980133818",
        trunk_prefix="0",
        timezone="America/Sao_Paulo",
        standard_utc_offset_minutes=-180,
        dst_utc_offset_minutes=-180,
        content_hash_fallback="759169e5b7de230616d673bd3498ac79",
        identity_document_type="CPF",
        include_crs_data=False,
        include_residential_address=False,
        marketing_opt_out=False,
        billing_autocomplete_type="ANS",
        billing_is_user_modified=True,
        include_card_product_class=True,
        include_date_of_birth=True,
        include_shipping_state=True,
        supports_postal_normalization=True,
        house_number_first=False,
    ),
    "GB": CountryProfile(
        code="GB",
        name="United Kingdom",
        locale="en_GB",
        language="en",
        accept_language="en-GB,en;q=0.9",
        currency="GBP",
        phone_calling_code="44",
        phone_local_min_length=9,
        phone_local_max_length=10,
        phone_example="+447911123456",
        trunk_prefix="0",
        timezone="Europe/London",
        standard_utc_offset_minutes=0,
        dst_utc_offset_minutes=60,
        content_hash_fallback="00bdb0e1f673c58b56ba490228ad3d59",
        identity_document_type=None,
        include_crs_data=True,
        include_residential_address=True,
        marketing_opt_out=False,
        billing_autocomplete_type="MANUAL",
        billing_is_user_modified=True,
        include_card_product_class=False,
        include_date_of_birth=True,
        include_shipping_state=False,
        supports_postal_normalization=False,
        house_number_first=True,
    ),
    "US": CountryProfile(
        code="US",
        name="United States",
        locale="en_US",
        language="en",
        accept_language="en-US,en;q=0.9",
        currency="USD",
        phone_calling_code="1",
        phone_local_min_length=10,
        phone_local_max_length=10,
        phone_example="+12125550123",
        trunk_prefix="",
        timezone="America/New_York",
        standard_utc_offset_minutes=-300,
        dst_utc_offset_minutes=-240,
        content_hash_fallback="846cec6423261581a1dac780262688f5",
        identity_document_type=None,
        include_crs_data=False,
        include_residential_address=False,
        marketing_opt_out=False,
        billing_autocomplete_type="GOOGLE",
        billing_is_user_modified=False,
        include_card_product_class=False,
        include_date_of_birth=False,
        include_shipping_state=True,
        supports_postal_normalization=False,
        house_number_first=True,
    ),
    "TH": CountryProfile(
        code="TH",
        name="Thailand",
        locale="th_TH",
        language="th",
        accept_language="th-TH,th;q=0.9,en;q=0.8",
        currency="THB",
        phone_calling_code="66",
        phone_local_min_length=9,
        phone_local_max_length=9,
        phone_example="+66812345678",
        trunk_prefix="0",
        timezone="Asia/Bangkok",
        standard_utc_offset_minutes=420,
        dst_utc_offset_minutes=420,
        content_hash_fallback="",
        identity_document_type=None,
        include_crs_data=True,
        include_residential_address=True,
        marketing_opt_out=False,
        billing_autocomplete_type="MANUAL",
        billing_is_user_modified=True,
        include_card_product_class=False,
        include_date_of_birth=True,
        include_shipping_state=True,
        supports_postal_normalization=False,
        house_number_first=True,
    ),
    "ID": CountryProfile(
        code="ID",
        name="Indonesia",
        locale="id_ID",
        language="id",
        accept_language="id-ID,id;q=0.9,en;q=0.8",
        currency="IDR",
        phone_calling_code="62",
        phone_local_min_length=9,
        phone_local_max_length=12,
        phone_example="+628123456789",
        trunk_prefix="0",
        timezone="Asia/Jakarta",
        standard_utc_offset_minutes=420,
        dst_utc_offset_minutes=420,
        content_hash_fallback="",
        identity_document_type=None,
        include_crs_data=True,
        include_residential_address=True,
        marketing_opt_out=False,
        billing_autocomplete_type="MANUAL",
        billing_is_user_modified=True,
        include_card_product_class=False,
        include_date_of_birth=True,
        include_shipping_state=True,
        supports_postal_normalization=False,
        house_number_first=False,
    ),
}

COUNTRY_ALIASES = {
    "BRAZIL": "BR",
    "UK": "GB",
    "UNITEDKINGDOM": "GB",
    "USA": "US",
    "UNITEDSTATES": "US",
    "THAILAND": "TH",
    "INDONESIA": "ID",
}

SUPPORTED_COUNTRY_CODES = tuple(COUNTRY_PROFILES)


def normalize_country(value: str) -> str:
    normalized = "".join(
        ch for ch in str(value or "BR").strip().upper() if ch.isalnum()
    )
    normalized = COUNTRY_ALIASES.get(normalized, normalized)
    if normalized not in COUNTRY_PROFILES:
        supported = ", ".join(SUPPORTED_COUNTRY_CODES)
        raise ValueError(f"unsupported country mode: {value!r}; expected one of {supported}")
    return normalized


def get_country_profile(value: str) -> CountryProfile:
    return COUNTRY_PROFILES[normalize_country(value)]


def normalize_phone(phone: str, country: str) -> tuple[str, str, str]:
    profile = get_country_profile(country)
    raw = str(phone or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        raise ValueError("phone number is required")

    international = raw.startswith("+") or digits.startswith("00")
    if digits.startswith("00"):
        digits = digits[2:]

    calling_code = profile.phone_calling_code
    remaining_length = len(digits) - len(calling_code)
    has_calling_code = digits.startswith(calling_code) and (
        profile.phone_local_min_length
        <= remaining_length
        <= profile.phone_local_max_length
    )

    if international:
        if not has_calling_code:
            raise ValueError(f"phone number does not match {profile.code} calling code +{calling_code}")
        local = digits[len(calling_code):]
    elif has_calling_code:
        local = digits[len(calling_code):]
    else:
        local = digits
        if profile.trunk_prefix and local.startswith(profile.trunk_prefix):
            without_trunk = local[len(profile.trunk_prefix):]
            if (
                profile.phone_local_min_length
                <= len(without_trunk)
                <= profile.phone_local_max_length
            ):
                local = without_trunk

    if not (
        profile.phone_local_min_length
        <= len(local)
        <= profile.phone_local_max_length
    ):
        expected = (
            str(profile.phone_local_min_length)
            if profile.phone_local_min_length == profile.phone_local_max_length
            else f"{profile.phone_local_min_length}-{profile.phone_local_max_length}"
        )
        raise ValueError(f"{profile.code} local phone number must contain {expected} digits")

    full = f"+{calling_code}{local}"
    return full, f"+{calling_code}", local
