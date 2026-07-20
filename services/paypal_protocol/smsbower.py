from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import os
import re
import time
import unicodedata
from typing import Any, Callable

import httpx

from .countries import normalize_country, normalize_phone


SMSBOWER_API_URL = "https://smsbower.page/stubs/handler_api.php"
SMSBOWER_SERVICE_NAME = "PayPal"
SMSBOWER_SUPPORTED_COUNTRIES = ("BR", "GB", "US", "TH", "ID")
SMSBOWER_ABSOLUTE_MAX_PRICE = Decimal(
    os.getenv("PAYPAL_SMSBOWER_MAX_PRICE_CAP", "10.00")
)

_COUNTRY_NAMES = {
    "BR": {"brazil", "brasil"},
    "GB": {"united kingdom", "great britain", "england"},
    "US": {"usa", "united states", "united states of america"},
    "TH": {"thailand", "kingdom of thailand"},
    "ID": {"indonesia", "republic of indonesia"},
}
_API_KEY_RE = re.compile(r"^[^\s]{8,256}$")


class SmsBowerError(RuntimeError):
    pass


class SmsBowerCodeTimeout(SmsBowerError):
    pass


@dataclass(frozen=True)
class SmsBowerActivation:
    activation_id: str
    phone_number: str
    activation_cost: Decimal
    country_id: int
    service_code: str


def validate_api_key(value: str) -> str:
    value = (value or "").strip()
    if not _API_KEY_RE.fullmatch(value):
        raise ValueError("SMSBower API key format is invalid")
    return value


def validate_max_price(value: Any) -> Decimal:
    try:
        price = Decimal(str(value)).quantize(Decimal("0.001"))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("SMSBower maximum price must be a number") from exc
    if price <= 0:
        raise ValueError("SMSBower maximum price must be greater than zero")
    if price > SMSBOWER_ABSOLUTE_MAX_PRICE:
        raise ValueError(
            f"SMSBower maximum price cannot exceed {SMSBOWER_ABSOLUTE_MAX_PRICE} USD"
        )
    return price


def _normalized_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).strip().casefold()


def _iter_records(payload: Any, collection_key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    nested = payload.get(collection_key)
    if isinstance(nested, list):
        return [item for item in nested if isinstance(item, dict)]
    if isinstance(nested, dict):
        return [item for item in nested.values() if isinstance(item, dict)]
    return [item for item in payload.values() if isinstance(item, dict)]


class SmsBowerClient:
    def __init__(
        self,
        api_key: str,
        max_price: Any,
        *,
        client: httpx.Client | None = None,
    ):
        self.api_key = validate_api_key(api_key)
        self.max_price = validate_max_price(max_price)
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            trust_env=False,
            headers={"Accept": "application/json,text/plain,*/*"},
        )
        self._service_code = ""
        self._country_ids: dict[str, int] = {}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _request(self, action: str, **params: Any) -> httpx.Response:
        query = {"api_key": self.api_key, "action": action}
        query.update({key: value for key, value in params.items() if value not in (None, "")})
        try:
            response = self.client.get(SMSBOWER_API_URL, params=query)
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            raise SmsBowerError(f"SMSBower {action} request failed") from exc

    @staticmethod
    def _error_from_text(value: str) -> str:
        text = (value or "").strip()
        known_errors = {
            "BAD_KEY": "SMSBower API key is invalid",
            "BAD_ACTION": "SMSBower rejected the API action",
            "BAD_SERVICE": "SMSBower PayPal service is unavailable",
            "BAD_COUNTRY": "SMSBower country is unavailable",
            "NO_NUMBERS": "SMSBower has no matching PayPal numbers within the price limit",
            "NO_BALANCE": "SMSBower balance is insufficient",
            "NO_ACTIVATION": "SMSBower activation does not exist",
            "MAX_PRICE_EXCEEDED": "SMSBower has no number within the configured maximum price",
        }
        for marker, message in known_errors.items():
            if marker in text.upper():
                return message
        return f"SMSBower returned an unexpected response: {text[:120]}"

    def _json(self, action: str, **params: Any) -> Any:
        response = self._request(action, **params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SmsBowerError(self._error_from_text(response.text)) from exc
        if isinstance(payload, dict) and str(payload.get("status", "")).casefold() == "error":
            message = payload.get("message") or payload.get("error") or response.text
            raise SmsBowerError(self._error_from_text(str(message)))
        return payload

    def get_balance(self) -> Decimal:
        text = self._request("getBalance").text.strip()
        if not text.startswith("ACCESS_BALANCE:"):
            raise SmsBowerError(self._error_from_text(text))
        try:
            return Decimal(text.split(":", 1)[1])
        except InvalidOperation as exc:
            raise SmsBowerError("SMSBower returned an invalid balance") from exc

    def resolve_paypal_service(self) -> str:
        if self._service_code:
            return self._service_code
        payload = self._json("getServicesList")
        for item in _iter_records(payload, "services"):
            if _normalized_name(item.get("name")) == "paypal":
                code = str(item.get("code") or "").strip()
                if code:
                    self._service_code = code
                    return code
        raise SmsBowerError("SMSBower PayPal service code was not found")

    def resolve_country_id(self, country: str) -> int:
        country = normalize_country(country)
        if country not in SMSBOWER_SUPPORTED_COUNTRIES:
            raise SmsBowerError(f"SMSBower country {country} is not allowed")
        if country in self._country_ids:
            return self._country_ids[country]

        payload = self._json("getCountries")
        allowed_names = _COUNTRY_NAMES[country]
        for item in _iter_records(payload, "countries"):
            names = {
                _normalized_name(item.get("eng")),
                _normalized_name(item.get("name")),
                _normalized_name(item.get("en")),
            }
            if country == "US" and any("virtual" in name for name in names):
                continue
            if not names.intersection(allowed_names):
                continue
            try:
                country_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            self._country_ids[country] = country_id
            return country_id
        raise SmsBowerError(f"SMSBower country mapping for {country} was not found")

    @staticmethod
    def _price_tiers(payload: Any) -> list[tuple[Decimal, int]]:
        tiers: list[tuple[Decimal, int]] = []

        def visit(value: Any) -> None:
            if not isinstance(value, dict):
                return
            if "cost" in value and "count" in value:
                try:
                    tiers.append((Decimal(str(value["cost"])), int(value["count"])))
                except (InvalidOperation, TypeError, ValueError):
                    pass
            for key, nested in value.items():
                if isinstance(nested, (int, float, str)):
                    try:
                        price = Decimal(str(key))
                        count = int(nested)
                    except (InvalidOperation, TypeError, ValueError):
                        continue
                    if price >= 0 and count >= 0:
                        tiers.append((price, count))
                elif isinstance(nested, dict):
                    visit(nested)

        visit(payload)
        return sorted(set(tiers), key=lambda item: item[0])

    def preview(self, country: str) -> dict[str, Any]:
        service_code = self.resolve_paypal_service()
        country_id = self.resolve_country_id(country)
        balance = self.get_balance()
        prices = self._json(
            "getPricesV2",
            service=service_code,
            country=country_id,
        )
        tiers = [
            {"price": float(price), "count": count}
            for price, count in self._price_tiers(prices)
            if price <= self.max_price and count > 0
        ]
        return {
            "service": SMSBOWER_SERVICE_NAME,
            "service_code": service_code,
            "country": normalize_country(country),
            "country_id": country_id,
            "balance": float(balance),
            "max_price": float(self.max_price),
            "lowest_price": tiers[0]["price"] if tiers else None,
            "available_count": sum(item["count"] for item in tiers),
        }

    def acquire_paypal_number(self, country: str) -> SmsBowerActivation:
        country = normalize_country(country)
        service_code = self.resolve_paypal_service()
        country_id = self.resolve_country_id(country)
        payload = self._json(
            "getNumberV2",
            service=service_code,
            country=country_id,
            maxPrice=format(self.max_price, "f"),
        )
        if not isinstance(payload, dict):
            raise SmsBowerError("SMSBower getNumberV2 returned invalid data")

        activation_id = str(payload.get("activationId") or "").strip()
        phone_number = str(payload.get("phoneNumber") or "").strip()
        try:
            activation_cost = Decimal(str(payload.get("activationCost")))
        except InvalidOperation as exc:
            raise SmsBowerError("SMSBower did not return the activation cost") from exc
        if not activation_id or not phone_number:
            raise SmsBowerError("SMSBower did not return an activation ID and phone number")

        phone_number = "+" + re.sub(r"\D", "", phone_number)
        try:
            phone_number, _, _ = normalize_phone(phone_number, country)
        except ValueError as exc:
            self.cancel(activation_id, ignore_errors=True)
            raise SmsBowerError("SMSBower returned a number outside the selected country") from exc
        if activation_cost > self.max_price:
            self.cancel(activation_id, ignore_errors=True)
            raise SmsBowerError(
                f"SMSBower activation cost {activation_cost} exceeds limit {self.max_price} USD"
            )

        return SmsBowerActivation(
            activation_id=activation_id,
            phone_number=phone_number,
            activation_cost=activation_cost,
            country_id=country_id,
            service_code=service_code,
        )

    def get_status(self, activation_id: str) -> str:
        return self._request("getStatus", id=activation_id).text.strip()

    def wait_for_code(
        self,
        activation_id: str,
        *,
        timeout_seconds: int = 180,
        poll_interval: float = 3.0,
        on_status: Callable[[str], None] | None = None,
    ) -> str:
        deadline = time.monotonic() + timeout_seconds
        last_status = ""
        while time.monotonic() < deadline:
            status = self.get_status(activation_id)
            if status != last_status and on_status:
                on_status(status.split(":", 1)[0])
            last_status = status
            if status.startswith("STATUS_OK:"):
                payload = status.split(":", 1)[1]
                match = re.search(r"(?<!\d)(\d{6})(?!\d)", payload)
                if not match:
                    raise SmsBowerError("SMSBower PayPal SMS did not contain a 6-digit code")
                return match.group(1)
            if status.startswith(("STATUS_WAIT_CODE", "STATUS_WAIT_RETRY")):
                time.sleep(poll_interval)
                continue
            if status.startswith("STATUS_CANCEL"):
                raise SmsBowerError("SMSBower activation was canceled")
            raise SmsBowerError(self._error_from_text(status))

        # Close the polling-boundary race: an SMS can arrive during the final
        # sleep immediately before the local deadline expires.
        status = self.get_status(activation_id)
        if status != last_status and on_status:
            on_status(status.split(":", 1)[0])
        if status.startswith("STATUS_OK:"):
            payload = status.split(":", 1)[1]
            match = re.search(r"(?<!\d)(\d{6})(?!\d)", payload)
            if not match:
                raise SmsBowerError("SMSBower PayPal SMS did not contain a 6-digit code")
            return match.group(1)
        if status.startswith("STATUS_CANCEL"):
            raise SmsBowerError("SMSBower activation was canceled")
        if not status.startswith(("STATUS_WAIT_CODE", "STATUS_WAIT_RETRY")):
            raise SmsBowerError(self._error_from_text(status))
        raise SmsBowerCodeTimeout("Timed out waiting for a PayPal SMS from SMSBower")

    def set_status(self, activation_id: str, status: int) -> str:
        response = self._request("setStatus", id=activation_id, status=status).text.strip()
        if response.startswith("ACCESS_"):
            return response
        raise SmsBowerError(self._error_from_text(response))

    def request_another_code(self, activation_id: str) -> str:
        return self.set_status(activation_id, 3)

    def complete(self, activation_id: str) -> str:
        return self.set_status(activation_id, 6)

    def cancel(self, activation_id: str, *, ignore_errors: bool = False) -> str:
        try:
            return self.set_status(activation_id, 8)
        except SmsBowerError:
            if ignore_errors:
                return ""
            raise
