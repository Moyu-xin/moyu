from __future__ import annotations

import json
import html
import ipaddress
import os
import queue
import random
import re
import secrets
import hashlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit, unquote

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from services.atomic_json_store import read_json, write_json

try:
    from curl_cffi.requests import Session as CurlCffiSession  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    CurlCffiSession = None  # type: ignore

try:
    from services.proxy_chain_service import proxy_chain_manager  # type: ignore
except Exception:  # pragma: no cover - standalone fallback
    proxy_chain_manager = None  # type: ignore


DEFAULT_STRIPE_PK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRac"
    "ViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
STRIPE_VERSION_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
STRIPE_VERSION_REFERENCE = "2020-08-27; elements_session_client_v1_beta_2025_01_02"
DEFAULT_TIMEOUT = 30
CHATGPT_TIMEOUT = 45
CHATGPT_RETRY_ATTEMPTS = 10
APPROVE_BLOCKED_BURST_ATTEMPTS = 50
APPROVE_BLOCKED_BURST_TIMEOUT_SECONDS = 1.0
PROVIDER_RETRY_ATTEMPTS = 10
PROXY_EXIT_RETRY_ATTEMPTS = 10
PROXY_EXIT_RETRY_DELAY_SECONDS = 5
APPROVE_NETWORK_RETRY_ATTEMPTS = 5
MAX_ACTIVE_RUN_JOBS = 3
MAX_BATCH_RESULT_BYTES = 2_000_000
EXPECTED_MATRIX_TOTAL = 24
EXPECTED_BILLING_COUNTRY_COUNT = 8
BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
DATA_DIR = Path(os.getenv("OPENAI_PAY_DATA_DIR", BASE_DIR / "data"))
LOG_DIR = Path(os.getenv("OPENAI_PAY_LOG_DIR", DATA_DIR / "logs"))
STATS_FILE = DATA_DIR / "stats.json"
STATS_LOCK = threading.Lock()
RUN_JOBS: dict[str, dict[str, Any]] = {}
RUN_JOBS_LOCK = threading.Lock()
RUN_JOB_TTL_SECONDS = 3600
RUN_JOB_MAX_ITEMS = 80
RUN_JOB_MAX_EVENTS = 500
LOCAL_RUN_KEY = os.getenv("OPENAI_PAY_LOCAL_RUN_KEY", "").strip() or secrets.token_urlsafe(24)
APPROVE_BURST_SEMAPHORE = threading.BoundedSemaphore(APPROVE_BLOCKED_BURST_ATTEMPTS)
CANCEL_CONTEXT = threading.local()
DEFAULT_PROXY = os.getenv(
    "OPENAI_PAY_DEFAULT_PROXY",
    "",
).strip()
PROVIDER_STAGE_PROXY = os.getenv("OPENAI_PAY_PROVIDER_PROXY", "").strip()
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"
US_BILLING_NAMES = [
    ("James", "Smith"),
    ("John", "Brown"),
    ("Michael", "Johnson"),
    ("Robert", "Miller"),
    ("David", "Davis"),
    ("William", "Wilson"),
]
US_BILLING_STREETS = [
    ("3110 Sunset Boulevard", "Los Angeles", "CA", "90026"),
    ("1200 Market Street", "San Francisco", "CA", "94102"),
    ("500 Main Street", "Austin", "TX", "78701"),
    ("88 Broadway", "New York", "NY", "10007"),
    ("1200 Peachtree St", "Atlanta", "GA", "30309"),
]
DE_BILLING_NAMES = [
    ("Lukas", "Schneider"),
    ("Felix", "Muller"),
    ("Jonas", "Weber"),
    ("Leon", "Fischer"),
    ("Marie", "Wagner"),
    ("Laura", "Becker"),
    ("Maximilian", "Hoffmann"),
    ("Paul", "Schulz"),
    ("Emma", "Koch"),
    ("Hannah", "Bauer"),
    ("Sophie", "Richter"),
    ("Noah", "Klein"),
]
DE_BILLING_STREETS = [
    ("Friedrichstrasse 123", "Berlin", "BE", "10117"),
    ("Leopoldstrasse 50", "Munich", "BY", "80802"),
    ("Zeil 85", "Frankfurt am Main", "HE", "60313"),
    ("Konigsallee 60", "Dusseldorf", "NW", "40212"),
    ("Moenckebergstrasse 7", "Hamburg", "HH", "20095"),
    ("Hohenzollernring 72", "Cologne", "NW", "50672"),
    ("Kaiserstrasse 44", "Stuttgart", "BW", "70173"),
    ("Kaufingerstrasse 15", "Munich", "BY", "80331"),
    ("Georgstrasse 24", "Hanover", "NI", "30159"),
    ("Prager Strasse 9", "Dresden", "SN", "01069"),
    ("Schadowstrasse 36", "Dusseldorf", "NW", "40212"),
    ("Breite Strasse 18", "Bonn", "NW", "53111"),
]
AU_BILLING_NAMES = [
    ("Jack", "Wilson"),
    ("Oliver", "Taylor"),
    ("Noah", "Brown"),
    ("Charlotte", "Smith"),
    ("Amelia", "Jones"),
    ("Isla", "Williams"),
]
AU_BILLING_STREETS = [
    ("120 Collins Street", "Melbourne", "Victoria", "3000"),
    ("88 George Street", "Sydney", "New South Wales", "2000"),
    ("45 Queen Street", "Brisbane", "Queensland", "4000"),
    ("22 King William Street", "Adelaide", "South Australia", "5000"),
    ("60 St Georges Terrace", "Perth", "Western Australia", "6000"),
    ("18 Elizabeth Street", "Hobart", "Tasmania", "7000"),
]
EXTRA_BILLING_NAMES = [
    ("Alex", "Tan"),
    ("Daniel", "Lee"),
    ("Emma", "Wong"),
    ("Mia", "Chen"),
    ("Noah", "Martin"),
    ("Olivia", "Nguyen"),
]
EXTRA_BILLING_STREETS = {
    "TH": [
        ("999 Rama I Road", "Bangkok", "Bangkok", "10330"),
        ("88 Sukhumvit Road", "Bangkok", "Bangkok", "10110"),
        ("45 Nimman Road", "Chiang Mai", "Chiang Mai", "50200"),
    ],
    "NZ": [
        ("22 Queen Street", "Auckland", "Auckland", "1010"),
        ("50 Lambton Quay", "Wellington", "Wellington", "6011"),
        ("120 Hereford Street", "Christchurch", "Canterbury", "8011"),
    ],
    "CA": [
        ("100 King Street West", "Toronto", "ON", "M5X 1A9"),
        ("555 West Hastings Street", "Vancouver", "BC", "V6B 4N6"),
        ("1250 Rene-Levesque Blvd", "Montreal", "QC", "H3B 4W8"),
    ],
    "BR": [
        ("100 Avenida Paulista", "Sao Paulo", "SP", "01310-100"),
        ("200 Avenida Atlantica", "Rio de Janeiro", "RJ", "22021-001"),
        ("50 Rua XV de Novembro", "Curitiba", "PR", "80020-310"),
    ],
    "IN": [
        ("12 MG Road", "Bengaluru", "KA", "560001"),
        ("88 Bandra Kurla Complex", "Mumbai", "MH", "400051"),
        ("45 Connaught Place", "New Delhi", "DL", "110001"),
    ],
    "EG": [
        ("15 Tahrir Square", "Cairo", "Cairo", "11512"),
        ("42 Corniche Road", "Alexandria", "Alexandria", "21599"),
        ("28 El Nasr Street", "Giza", "Giza", "12511"),
    ],
    "ID": [
        ("10 Sudirman Avenue", "Jakarta", "DKI Jakarta", "10220"),
        ("55 Asia Afrika Street", "Bandung", "West Java", "40111"),
        ("23 Basuki Rahmat", "Surabaya", "East Java", "60271"),
    ],
    "MX": [
        ("120 Paseo de la Reforma", "Mexico City", "CDMX", "06600"),
        ("45 Avenida Vallarta", "Guadalajara", "JAL", "44100"),
        ("88 Constitucion Avenue", "Monterrey", "NL", "64000"),
    ],
    "CO": [
        ("30 Carrera 7", "Bogota", "Bogota", "110111"),
        ("18 Calle 10", "Medellin", "Antioquia", "050021"),
        ("76 Avenida 6N", "Cali", "Valle del Cauca", "760045"),
    ],
    "ZA": [
        ("100 Commissioner Street", "Johannesburg", "Gauteng", "2001"),
        ("50 Long Street", "Cape Town", "Western Cape", "8001"),
        ("77 Florida Road", "Durban", "KwaZulu-Natal", "4001"),
    ],
    "FR": [
        ("10 Rue de Rivoli", "Paris", "Ile-de-France", "75001"),
        ("25 Rue de la Republique", "Lyon", "Auvergne-Rhone-Alpes", "69002"),
        ("40 La Canebiere", "Marseille", "Provence-Alpes-Cote d'Azur", "13001"),
    ],
    "IT": [
        ("15 Via Roma", "Rome", "Lazio", "00184"),
        ("80 Corso Buenos Aires", "Milan", "Lombardy", "20124"),
        ("22 Via Po", "Turin", "Piedmont", "10124"),
    ],
    "ES": [
        ("20 Gran Via", "Madrid", "Madrid", "28013"),
        ("30 Passeig de Gracia", "Barcelona", "Catalonia", "08007"),
        ("12 Calle Colon", "Valencia", "Valencian Community", "46004"),
    ],
    "NL": [
        ("100 Damrak", "Amsterdam", "North Holland", "1012 LP"),
        ("25 Coolsingel", "Rotterdam", "South Holland", "3012 AA"),
        ("8 Oudegracht", "Utrecht", "Utrecht", "3511 AB"),
    ],
    "DK": [
        ("12 Stroget", "Copenhagen", "Capital Region", "1160"),
        ("45 Sonder Alle", "Aarhus", "Central Denmark", "8000"),
        ("9 Vestergade", "Odense", "Southern Denmark", "5000"),
    ],
    "KR": [
        ("50 Teheran-ro", "Seoul", "Seoul", "06236"),
        ("20 Centumjungang-ro", "Busan", "Busan", "48059"),
        ("77 Songdo Gukje-daero", "Incheon", "Incheon", "21984"),
    ],
    "AE": [
        ("10 Sheikh Zayed Road", "Dubai", "Dubai", "00000"),
        ("25 Corniche Street", "Abu Dhabi", "Abu Dhabi", "00000"),
        ("18 Al Majaz Road", "Sharjah", "Sharjah", "00000"),
    ],
    "IL": [
        ("45 Rothschild Boulevard", "Tel Aviv", "Tel Aviv", "6688312"),
        ("12 Jaffa Road", "Jerusalem", "Jerusalem", "9434107"),
        ("30 Hanassi Avenue", "Haifa", "Haifa", "3463412"),
    ],
    "JP": [
        ("1-1 Marunouchi", "Tokyo", "Tokyo", "100-0005"),
        ("2-2-1 Nakanoshima", "Osaka", "Osaka", "530-0005"),
        ("1-1 Minatomirai", "Yokohama", "Kanagawa", "220-0012"),
    ],
    "RO": [
        ("10 Calea Victoriei", "Bucharest", "Bucharest", "010061"),
        ("22 Strada Memorandumului", "Cluj-Napoca", "Cluj", "400114"),
        ("55 Piata Unirii", "Timisoara", "Timis", "300085"),
    ],
}
COUNTRY_CURRENCY = {
    "AE": "AED",
    "AU": "AUD",
    "BR": "BRL",
    "CA": "CAD",
    "CO": "COP",
    "DK": "DKK",
    "DE": "EUR",
    "EG": "EGP",
    "ES": "EUR",
    "FR": "EUR",
    "GB": "GBP",
    "ID": "IDR",
    "IL": "ILS",
    "IN": "INR",
    "IT": "EUR",
    "JP": "JPY",
    "KR": "KRW",
    "MX": "MXN",
    "NL": "EUR",
    "NZ": "NZD",
    "RO": "RON",
    "ZA": "ZAR",
    "TH": "THB",
    "TR": "USD",
    "US": "USD",
    "VN": "VND",
}
OPENAI_SUPPORTED_COUNTRY_CODES = {
    "US", "GB", "BR", "IN", "EG", "ID", "TH", "MX", "CO", "ZA", "DE", "FR",
    "IT", "ES", "NL", "DK", "CA", "AU", "NZ", "KR", "AE", "IL", "JP", "RO", "TR", "VN",
}
RUN_MODE_COUNTRIES = set(OPENAI_SUPPORTED_COUNTRY_CODES)
PROVIDER_COUNTRY_BY_MODE = {
    "AE": "AE",
    "AU": "AU",
    "BR": "BR",
    "CA": "CA",
    "CO": "CO",
    "DE": "DE",
    "DK": "DK",
    "EG": "EG",
    "ES": "ES",
    "FR": "FR",
    "GB": "GB",
    "ID": "ID",
    "IL": "IL",
    "IN": "IN",
    "IT": "IT",
    "JP": "JP",
    "KR": "KR",
    "MX": "MX",
    "NL": "NL",
    "NZ": "NZ",
    "RO": "RO",
    "ZA": "ZA",
    "TH": "TH",
    "TR": "TR",
    "US": "US",
    "VN": "VN",
}
COUNTRY_PHONE_PREFIX = {
    "AE": "+971",
    "AU": "+61",
    "BR": "+55",
    "CA": "+1",
    "CO": "+57",
    "DK": "+45",
    "DE": "+49",
    "EG": "+20",
    "ES": "+34",
    "FR": "+33",
    "GB": "+44",
    "ID": "+62",
    "IL": "+972",
    "IN": "+91",
    "IT": "+39",
    "JP": "+81",
    "KR": "+82",
    "MX": "+52",
    "NL": "+31",
    "NZ": "+64",
    "RO": "+40",
    "ZA": "+27",
    "TH": "+66",
    "TR": "+90",
    "US": "+1",
    "VN": "+84",
}
BILLING_PROFILE_CITY_BY_COUNTRY = {
    "BR": ["Sao Paulo", "Rio de Janeiro", "Curitiba"],
    "CO": ["Bogota", "Medellin", "Cali"],
    "DK": ["Copenhagen", "Aarhus", "Odense"],
    "EG": ["Cairo", "Alexandria", "Giza"],
    "ES": ["Madrid", "Barcelona", "Valencia"],
    "FR": ["Paris", "Lyon", "Marseille"],
    "GB": ["London", "Manchester", "Birmingham"],
    "ID": ["Jakarta", "Bandung", "Surabaya"],
    "IL": ["Tel Aviv", "Jerusalem", "Haifa"],
    "IN": ["Mumbai", "Delhi", "Bengaluru"],
    "IT": ["Rome", "Milan", "Turin"],
    "JP": ["Tokyo", "Osaka", "Yokohama"],
    "KR": ["Seoul", "Busan", "Incheon"],
    "MX": ["Mexico City", "Guadalajara", "Monterrey"],
    "NL": ["Amsterdam", "Rotterdam", "Utrecht"],
    "RO": ["Bucharest", "Cluj-Napoca", "Timisoara"],
    "TR": ["Istanbul", "Ankara", "Izmir"],
    "VN": ["Ho Chi Minh City", "Hanoi", "Da Nang"],
    "ZA": ["Johannesburg", "Cape Town", "Durban"],
}
POSTAL_PATTERN_BY_COUNTRY = {
    "AE": "#####", "AU": "####", "BR": "#####-###", "CA": "A#A #A#", "CO": "######",
    "DE": "#####", "DK": "####", "EG": "#####", "ES": "#####", "GB": "AA# #AA",
    "FR": "#####", "ID": "#####", "IL": "#######", "IN": "######",
    "IT": "#####", "JP": "###-####", "KR": "#####", "MX": "#####",
    "NL": "#### AA", "NZ": "####", "RO": "######", "TH": "#####", "TR": "#####", "VN": "######",
    "US": "#####", "ZA": "####",
}
BILLING_STREET_POOL = [
    "Market Street",
    "Central Avenue",
    "Station Road",
    "Main Street",
    "High Street",
    "King Street",
]
BILLING_PROFILE_BY_COUNTRY = {
    country: {
        "currency": COUNTRY_CURRENCY.get(country, "USD"),
        "phone_prefix": COUNTRY_PHONE_PREFIX.get(country, "+1"),
        "city_pool": BILLING_PROFILE_CITY_BY_COUNTRY.get(country, ["Capital City", "Central District", "Market Town"]),
        "state_mode": "country_code",
        "postal_pattern": POSTAL_PATTERN_BY_COUNTRY.get(country, "#####"),
        "street_pool": BILLING_STREET_POOL,
        "source": "formatted",
    }
    for country in OPENAI_SUPPORTED_COUNTRY_CODES
}
for precise_country in ("US", "AU", "DE", "TH", "NZ", "CA"):
    if precise_country in BILLING_PROFILE_BY_COUNTRY:
        BILLING_PROFILE_BY_COUNTRY[precise_country]["source"] = "precise"
LOCALE_MAP = {
    "de": ("de-DE", "de"),
    "en": ("en-US", "en"),
    "en-US": ("en-US", "en"),
    "es": ("es-ES", "es"),
    "fr": ("fr-FR", "fr"),
    "id": ("id-ID", "id"),
    "it": ("it-IT", "it"),
    "ja": ("ja-JP", "ja"),
    "ko": ("ko-KR", "ko"),
    "pt-BR": ("pt-BR", "pt-BR"),
    "zh-CN": ("zh-CN", "zh-CN"),
    "zh-TW": ("zh-TW", "zh-TW"),
}



class LongLinkRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    access_token: str = Field(..., alias="accessToken")
    proxy: str = ""
    jp_proxy: str = ""
    us_proxy: str = ""
    au_proxy: str = ""
    gb_proxy: str = ""
    stripe_publishable_key: str = ""
    billing_country: str = "US"
    payment_method_country: str = "US"
    promotion_country: str = "TR"
    payment_method_country_pool: list[str] = Field(default_factory=list)
    run_mode: str = "combo_fallback"
    checkout_ui_mode: str = "custom"
    payment_locale: str = "en"
    enable_approve_blocked_burst: bool = True
    single_proxy_mode: bool = False
    all_jp_proxy: bool = False
    checkout_proxy_override: str = ""
    provider_proxy_override: str = ""
    approve_proxy_override: str = ""
    promotion_proxy_override: str = ""
    promotion_proxy_country: str = ""
    checkout_proxy_pool: list[str] = Field(default_factory=list)
    provider_proxy_pool: list[str] = Field(default_factory=list)
    approve_proxy_pool: list[str] = Field(default_factory=list)
    promotion_proxy_pool: list[str] = Field(default_factory=list)
    approve_same_as_checkout: bool = False
    provider_same_as_target: bool = True
    residential_via_clash: bool = False
    residential_clash_proxy: str = ""
    batch_target_country: str = ""
    batch_checkout_proxy_country: str = ""
    require_zero_stripe_amount: bool = True
    checkout_retry_limit: int = Field(CHATGPT_RETRY_ATTEMPTS, ge=1, le=50)
    provider_retry_limit: int = Field(PROXY_EXIT_RETRY_ATTEMPTS, ge=1, le=20)
    strict_same_proxy_ip: bool = True
    strict_approve_same_proxy_ip: bool = False
    device_id: str = ""
    user_agent: str = ""


class LongLinkResponse(BaseModel):
    ok: bool
    cs_id: str
    processor_entity: str
    billing_country: str
    payment_method_country: str
    promotion_country: str = ""
    currency: str
    payment_locale: str
    flow_type: str
    payment_method_type: str
    payment_method_id: str
    stripe_redirect_url: str
    provider_redirect_url: str
    fallback: bool = False
    provider_error: str = ""
    stripe_hosted_url: str
    long_url: str
    stripe_amount: str = ""
    stripe_amount_source: str = ""
    link_attempt_count: int = 0
    link_attempt_limit: int = 0


class ProxyCheckRequest(BaseModel):
    jp_proxy: str = ""
    us_proxy: str = ""
    au_proxy: str = ""
    gb_proxy: str = ""
    billing_country: str = "US"
    run_mode: str = "combo_fallback"
    single_proxy_mode: bool = False
    all_jp_proxy: bool = False


class ProxyProbeResult(BaseModel):
    ok: bool
    label: str
    proxy: str
    ip: str = ""
    country: str = ""
    country_code: str = ""
    error: str = ""


class ProxyCheckResponse(BaseModel):
    ok: bool
    results: list[ProxyProbeResult]


class BatchResultSaveRequest(BaseModel):
    filename: str
    payload: dict[str, Any]


class BatchResultSaveResponse(BaseModel):
    ok: bool
    filename: str
    path: str


def new_session() -> Any:
    if CurlCffiSession is not None:
        session = CurlCffiSession(impersonate="chrome136")
    else:
        session = requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    return session


def effective_default_proxy(proxy: str = "") -> str:
    return str(proxy or "").strip() or DEFAULT_PROXY


def checkout_stage_proxy(req: LongLinkRequest) -> str:
    override = str(getattr(req, "checkout_proxy_override", "") or "").strip()
    if override:
        return normalize_proxy_url(override)
    return normalize_proxy_url(str(req.jp_proxy or req.proxy or "").strip() or DEFAULT_PROXY)


def approve_stage_proxy(req: LongLinkRequest) -> str:
    if bool(getattr(req, "approve_same_as_checkout", False)):
        return checkout_stage_proxy(req)
    override = str(getattr(req, "approve_proxy_override", "") or "").strip()
    if override:
        return normalize_proxy_url(override)
    return checkout_stage_proxy(req)


def promotion_stage_proxy(req: LongLinkRequest) -> str:
    override = str(getattr(req, "promotion_proxy_override", "") or "").strip()
    if override:
        return normalize_proxy_url(override)
    return provider_stage_proxy(req)


def promotion_stage_country(req: LongLinkRequest) -> str:
    explicit = str(getattr(req, "promotion_proxy_country", "") or "").strip().upper()
    if explicit:
        return explicit
    override = str(getattr(req, "promotion_proxy_override", "") or "").strip()
    if override:
        return infer_proxy_country(override, effective_promotion_country(req))
    return effective_promotion_country(req)


def infer_proxy_country(proxy: str, default: str = "JP") -> str:
    proxy = normalize_proxy_url(proxy)
    default = str(default or "JP").strip().upper()
    try:
        parsed = urlsplit(proxy)
    except Exception:
        return default
    parts = [unquote(parsed.username or ""), unquote(parsed.password or ""), parsed.hostname or ""]
    text = " ".join(parts).upper()
    for country in sorted(RUN_MODE_COUNTRIES, key=len, reverse=True):
        if re.search(rf"(?:^|[-_:\s]){country}(?:$|[-_:\s])", text):
            return country
    return default


def checkout_stage_country(req: LongLinkRequest) -> str:
    explicit = str(getattr(req, "batch_checkout_proxy_country", "") or "").strip().upper()
    if explicit:
        return explicit
    override = str(getattr(req, "checkout_proxy_override", "") or "").strip()
    if override:
        return infer_proxy_country(override, "JP")
    return "JP"


def approve_stage_country(req: LongLinkRequest) -> str:
    if bool(getattr(req, "approve_same_as_checkout", False)):
        return checkout_stage_country(req)
    explicit = str(getattr(req, "batch_target_country", "") or "").strip().upper()
    if explicit:
        return explicit
    override = str(getattr(req, "approve_proxy_override", "") or "").strip()
    if override:
        return infer_proxy_country(override, "JP")
    return "JP"


def provider_same_as_target(req: Any) -> bool:
    return bool(getattr(req, "provider_same_as_target", True))


def provider_uses_checkout(req: Any) -> bool:
    return bool(getattr(req, "approve_same_as_checkout", False)) and not provider_same_as_target(req)


def normalized_run_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode.startswith("paypal_country:"):
        country = mode.split(":", 1)[1].upper()
        if country in RUN_MODE_COUNTRIES:
            return f"paypal_country:{country.lower()}"
    return mode


def paypal_country_mode_country(req: LongLinkRequest | ProxyCheckRequest) -> str:
    mode = normalized_run_mode(getattr(req, "run_mode", ""))
    if mode.startswith("paypal_country:"):
        return mode.split(":", 1)[1].upper()
    return ""


def provider_stage_country(req: LongLinkRequest | ProxyCheckRequest) -> str:
    if single_proxy_mode(req):
        return "SAME"
    if provider_uses_checkout(req):
        return checkout_stage_country(req)
    explicit = str(getattr(req, "batch_target_country", "") or "").strip().upper()
    if explicit:
        return explicit
    override = str(getattr(req, "provider_proxy_override", "") or "").strip()
    if override:
        return infer_proxy_country(override, "US")
    mode_country = paypal_country_mode_country(req)
    return PROVIDER_COUNTRY_BY_MODE.get(mode_country, "US")


def provider_stage_label(req: LongLinkRequest | ProxyCheckRequest) -> str:
    if single_proxy_mode(req):
        return "统一代理"
    return f"provider {provider_stage_country(req)}"


def single_proxy_mode(req: Any) -> bool:
    return bool(getattr(req, "single_proxy_mode", False) or getattr(req, "all_jp_proxy", False))


def clean_proxy_input(proxy: str) -> str:
    proxy = str(proxy or "").strip()
    if not proxy:
        return ""
    proxy = (
        proxy.replace("\uFF20", "@")
        .replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2015", "-")
        .replace("\u2212", "-")
        .replace("\uFE63", "-")
        .replace("\uFF0D", "-")
    )
    proxy = re.sub(r"[\u200B-\u200D\uFEFF]", "", proxy)
    proxy = re.sub(r"\s*@\s*", "@", proxy).strip().strip("'\"")

    env_match = re.match(
        r"^(?:export\s+|set\s+)?(?:HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|http_proxy|https_proxy|all_proxy)\s*=\s*(.+)$",
        proxy,
        flags=re.IGNORECASE,
    )
    if env_match:
        return clean_proxy_input(env_match.group(1))

    cli_match = re.search(r"(?:^|\s)(?:--proxy|-x)\s+('([^']+)'|\"([^\"]+)\"|(\S+))", proxy, flags=re.IGNORECASE)
    if cli_match:
        return clean_proxy_input(cli_match.group(2) or cli_match.group(3) or cli_match.group(4) or "")
    return proxy


def proxy_host_port(host: str, port: str) -> str:
    host = str(host or "").strip().strip("[]")
    port = str(port or "").strip()
    if not host or not port:
        return ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}"


def build_proxy_url(scheme: str, username: str, password: str, host: str, port: str) -> str:
    scheme = (scheme or "http").lower()
    if scheme in {"scok5", "sock5", "socks"}:
        scheme = "socks5"
    host_port = proxy_host_port(host, port)
    if not host_port:
        return ""
    if username:
        auth = f"{quote(str(username), safe='-._~')}:{quote(str(password or ''), safe='-._~')}@"
    else:
        auth = ""
    return f"{scheme}://{auth}{host_port}"


def normalize_proxy_url(proxy: str) -> str:
    proxy = clean_proxy_input(proxy)
    if not proxy:
        return ""

    proxy = re.sub(r"^scok5://", "socks5://", proxy, flags=re.IGNORECASE)
    proxy = re.sub(r"^sock5://", "socks5://", proxy, flags=re.IGNORECASE)
    proxy = re.sub(r"^socks://", "socks5://", proxy, flags=re.IGNORECASE)
    if "://" in proxy:
        parsed = urlsplit(proxy)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https", "socks4", "socks5", "socks5h"}:
            return proxy
        try:
            port = str(parsed.port or "")
        except ValueError:
            return proxy
        normalized = build_proxy_url(scheme, unquote(parsed.username or ""), unquote(parsed.password or ""), parsed.hostname or "", port)
        return normalized or proxy

    if "@" in proxy:
        return normalize_proxy_url(f"http://{proxy}")

    parts = proxy.split(":")
    if len(parts) >= 4 and parts[1].isdigit():
        host, port = parts[0], parts[1]
        username = parts[2]
        password = ":".join(parts[3:])
        return build_proxy_url("http", username, password, host, port) or f"http://{proxy}"
    if len(parts) >= 4 and parts[-1].isdigit():
        username, password = parts[0], parts[1]
        host = ":".join(parts[2:-1])
        port = parts[-1]
        return build_proxy_url("http", username, password, host, port) or f"http://{proxy}"
    if re.match(r"^[^:/\s]+:\d+$", proxy):
        return f"http://{proxy}"
    return f"http://{proxy}"


def chain_parent_proxy(req: Any) -> str:
    if not bool(getattr(req, "residential_via_clash", False)):
        return ""
    return normalize_proxy_url(str(getattr(req, "residential_clash_proxy", "") or "").strip())


def _proxy_url_for_session(proxy: str, parent_proxy: str = "") -> str:
    proxy = normalize_proxy_url(proxy)
    parent_proxy = normalize_proxy_url(parent_proxy)
    if proxy and parent_proxy and proxy_chain_manager is not None:
        try:
            return proxy_chain_manager.proxy_for(proxy, parent_proxy)
        except Exception:
            return proxy
    return proxy


def set_proxy_url(session: Any, proxy: str, parent_proxy: str = "") -> None:
    proxy = _proxy_url_for_session(proxy, parent_proxy)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}


def set_proxy(session: Any, proxy: str) -> None:
    set_proxy_url(session, effective_default_proxy(proxy))


def proxy_for_region(proxy: str, region: str) -> str:
    proxy = str(proxy or "").strip()
    region = str(region or "").strip().upper()
    if proxy and region and "region-" in proxy:
        return re.sub(r"region-[A-Za-z]{2,3}", f"region-{region}", proxy)
    return proxy


def new_proxy_session_id() -> str:
    return str(random.randint(10_000_000, 99_999_999))


def fill_proxy_template(proxy: str, *, country: str, session_id: str, stage: str = "", slot: int = 0) -> str:
    country = str(country or "").strip().upper()
    session_id = str(session_id or new_proxy_session_id()).strip()
    replacements = {
        "{country}": country,
        "{COUNTRY}": country,
        "{country_lower}": country.lower(),
        "{session}": session_id,
        "{session_id}": session_id,
        "{sticky}": session_id,
        "{stage}": str(stage or ""),
        "{slot}": str(max(0, int(slot or 0))),
        "{index}": str(max(0, int(slot or 0))),
    }
    text = str(proxy or "").strip()
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def proxy_template_has_explicit_session(proxy: str) -> bool:
    return any(token in str(proxy or "") for token in ("{session}", "{session_id}", "{sticky}"))


def rotate_711_proxy_username(username: str, country: str, session_id: str | None = None) -> str:
    username = str(username or "")
    country = str(country or "").strip().upper()
    if not username or not country:
        return username

    if re.search(r"region-[A-Za-z]{2,3}", username):
        rotated = re.sub(r"region-[A-Za-z]{2,3}", f"region-{country}", username, count=1, flags=re.IGNORECASE)
    elif "-zone-custom" in username.lower():
        rotated = f"{username}-region-{country}"
    else:
        return username
    session_id = str(session_id or new_proxy_session_id())
    if re.search(r"-session-[A-Za-z0-9]+", rotated, flags=re.IGNORECASE):
        return re.sub(r"-session-[A-Za-z0-9]+", f"-session-{session_id}", rotated, count=1, flags=re.IGNORECASE)
    if re.search(r"-sessTime-", rotated, flags=re.IGNORECASE):
        return re.sub(r"(?=-sessTime-)", f"-session-{session_id}", rotated, count=1, flags=re.IGNORECASE)
    return f"{rotated}-session-{session_id}-sessTime-5-sessAuto-1"


def rotate_kookeey_proxy_session(proxy: str, country: str, *, stage: str = "", slot: int = 0) -> str:
    original_proxy = str(proxy or "")
    session_id = new_proxy_session_id()
    proxy = normalize_proxy_url(fill_proxy_template(original_proxy, country=country, session_id=session_id, stage=stage, slot=slot))
    country = str(country or "").strip().upper()
    if not proxy or not country:
        return proxy
    if proxy_template_has_explicit_session(original_proxy):
        return proxy

    parsed = urlsplit(proxy)
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not username or not password:
        return proxy

    hostname = parsed.hostname or ""
    if not hostname:
        return proxy
    host = hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"

    is_711_proxy = bool(
        re.search(r"region-[A-Za-z]{2,3}", username)
        or ("-zone-custom" in username.lower() and "711proxy" in hostname.lower())
    )
    if is_711_proxy:
        # 711Proxy encodes country and sticky session in the username. Its password must stay exact.
        rotated_username = rotate_711_proxy_username(username, country, session_id=session_id)
        netloc = f"{quote(rotated_username, safe='-._~')}:{quote(password, safe='-._~')}@{host}"
        return urlunsplit((parsed.scheme or "http", netloc, parsed.path, parsed.query, parsed.fragment))

    match = re.match(r"^(?P<base>.+?)-(?P<country>global|[A-Za-z]{2,3})(?:-[A-Za-z0-9]+)?$", password, flags=re.IGNORECASE)
    if match:
        password_base = match.group("base")
    else:
        password_base = password
    rotated_password = f"{password_base}-{country}-{session_id}"

    netloc = f"{quote(username, safe='-._~')}:{quote(rotated_password, safe='-._~')}@{host}"
    return urlunsplit((parsed.scheme or "http", netloc, parsed.path, parsed.query, parsed.fragment))


def split_stage_proxy_pool(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = [str(item or "") for item in value]
    else:
        raw_items = str(value or "").replace("\r", "\n").split("\n")
    items: list[str] = []
    for raw in raw_items:
        line = str(raw or "").strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line and not line.lower().startswith(("http://", "https://", "socks://", "socks5://", "socks5h://")):
            _name, line = [part.strip() for part in line.split("=", 1)]
        if line:
            items.append(line)
    return items


def stage_proxy_pool(req: LongLinkRequest, stage: str) -> list[str]:
    stage = str(stage or "").strip().lower()
    if stage == "checkout":
        return split_stage_proxy_pool(getattr(req, "checkout_proxy_pool", []))
    if stage == "provider":
        return split_stage_proxy_pool(getattr(req, "provider_proxy_pool", []))
    if stage == "approve":
        return split_stage_proxy_pool(getattr(req, "approve_proxy_pool", []))
    if stage == "promotion":
        return split_stage_proxy_pool(getattr(req, "promotion_proxy_pool", []))
    return []


def random_stage_proxy_base(req: LongLinkRequest, stage: str, fallback: str, used_bases: set[str] | None = None) -> tuple[str, int, int]:
    pool = stage_proxy_pool(req, stage)
    if not pool:
        return str(fallback or "").strip(), 0, 0
    used = used_bases if used_bases is not None else set()
    candidates = [(idx, item) for idx, item in enumerate(pool) if item not in used]
    if not candidates:
        candidates = list(enumerate(pool))
    slot, proxy = random.choice(candidates)
    if used_bases is not None:
        used_bases.add(proxy)
    return proxy, slot + 1, len(pool)


def rotated_stage_proxy_for_attempt(req: LongLinkRequest, stage: str, country: str, fallback: str, used_bases: set[str] | None = None) -> str:
    base, slot, _size = random_stage_proxy_base(req, stage, fallback, used_bases=used_bases)
    if not base:
        return ""
    return rotate_kookeey_proxy_session(base, country, stage=stage, slot=slot)


def request_with_stage_proxy(req: LongLinkRequest, stage: str, proxy: str, country: str) -> LongLinkRequest:
    stage = str(stage or "").strip().lower()
    if stage == "checkout":
        field = "checkout_proxy_override" if str(getattr(req, "checkout_proxy_override", "") or "").strip() else "jp_proxy"
        return req.model_copy(update={field: proxy})
    if stage == "approve":
        field = "approve_proxy_override" if str(getattr(req, "approve_proxy_override", "") or "").strip() else "jp_proxy"
        return req.model_copy(update={field: proxy})
    if stage == "provider":
        update = (
            {"provider_proxy_override": proxy}
            if str(getattr(req, "provider_proxy_override", "") or "").strip()
            else {provider_proxy_field(country): proxy}
        )
        return req.model_copy(update=update)
    if stage == "promotion":
        return req.model_copy(
            update={
                "promotion_proxy_override": proxy,
                "promotion_proxy_country": country,
            }
        )
    return req


def request_with_rotated_proxy_sessions(req: LongLinkRequest) -> LongLinkRequest:
    if single_proxy_mode(req):
        return req.model_copy(update={"jp_proxy": checkout_stage_proxy(req)})
    provider_country = provider_stage_country(req)
    provider_proxy = rotated_stage_proxy_for_attempt(req, "provider", provider_country, provider_stage_proxy(req))
    checkout_country = checkout_stage_country(req)
    checkout_proxy = rotated_stage_proxy_for_attempt(req, "checkout", checkout_country, checkout_stage_proxy(req))
    next_req = request_with_stage_proxy(req, "checkout", checkout_proxy, checkout_country)
    return request_with_stage_proxy(next_req, "provider", provider_proxy, provider_country)


def request_with_rotated_checkout_session(req: LongLinkRequest, used_bases: set[str] | None = None) -> LongLinkRequest:
    if single_proxy_mode(req):
        return req.model_copy(update={"jp_proxy": checkout_stage_proxy(req)})
    country = checkout_stage_country(req)
    proxy = rotated_stage_proxy_for_attempt(req, "checkout", country, checkout_stage_proxy(req), used_bases=used_bases)
    return request_with_stage_proxy(req, "checkout", proxy, country)


def request_with_rotated_approve_session(req: LongLinkRequest, used_bases: set[str] | None = None) -> LongLinkRequest:
    if single_proxy_mode(req):
        return req.model_copy(update={"jp_proxy": approve_stage_proxy(req)})
    country = approve_stage_country(req)
    proxy = rotated_stage_proxy_for_attempt(req, "approve", country, approve_stage_proxy(req), used_bases=used_bases)
    return request_with_stage_proxy(req, "approve", proxy, country)


def provider_proxy_field(country: str) -> str:
    country = str(country or "").upper()
    if country in {"JP", "SAME"}:
        return "jp_proxy"
    if country == "AU":
        return "au_proxy"
    return "us_proxy"


def is_retryable_network_error(exc: Exception) -> bool:
    retryable_names = {
        "ReadTimeout",
        "ConnectTimeout",
        "ConnectionError",
        "Timeout",
        "SSLError",
        "ProxyError",
        "RemoteDisconnected",
        "ConnectionResetError",
    }
    for item in type(exc).mro():
        if item.__name__ in retryable_names:
            return True
    text = str(exc).lower()
    retryable_markers = (
        "read timed out",
        "connect timed out",
        "connection aborted",
        "connection reset",
        "remote end closed connection",
        "remote disconnected",
        "unexpected_eof_while_reading",
        "eof occurred in violation of protocol",
        "ssleoferror",
        "max retries exceeded",
        "proxyerror",
        "unable to connect to proxy",
        "operation timed out",
        "connect tunnel failed",
        "tls connect error",
        "curl: (28)",
        "curl: (35)",
        "curl: (56)",
    )
    return any(marker in text for marker in retryable_markers)


def proxy_ip_matches(expected_ip: str, actual_ip: str) -> bool:
    expected = str(expected_ip or "").strip().lower()
    actual = str(actual_ip or "").strip().lower()
    return bool(expected and actual and expected == actual)


def html_response_title(text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", str(text or ""), flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return re.sub(r"\\s+", " ", match.group(1)).strip()[:160]


def http_error_detail(prefix: str, response: Any, limit: int = 1200) -> str:
    body_text = str(getattr(response, "text", "") or "")
    content_type = str(getattr(response, "headers", {}).get("content-type", "") or "")
    title = html_response_title(body_text)
    parts = [
        f"{prefix}: status={getattr(response, 'status_code', '')}",
        f"content_type={content_type or 'unknown'}",
    ]
    if title:
        parts.append(f"title={title}")
    compact = re.sub(r"\\s+", " ", body_text).strip()
    if compact:
        parts.append(f"body={compact[:limit]}")
    return "；".join(parts)


def is_retryable_chatgpt_http_exception(exc: HTTPException) -> bool:
    detail = str(exc.detail or "")
    lowered = detail.lower()
    if exc.status_code in (429, 500, 502, 503, 504):
        return True
    if exc.status_code == 403 and ("<html" in lowered or "<!doctype html" in lowered or "text/html" in lowered):
        return True
    retryable_markers = (
        "checkout create failed",
        "chatgpt approve failed",
        "cloudflare",
        "cf-ray",
        "rate limited",
        "temporarily unavailable",
    )
    return exc.status_code in (403, 429) and any(marker in lowered for marker in retryable_markers)


def is_retryable_provider_http_exception(exc: HTTPException) -> bool:
    if exc.status_code not in (502, 504):
        return False
    detail = str(exc.detail or "")
    return is_retryable_network_error(Exception(detail))


def is_retryable_provider_flow_exception(exc: HTTPException) -> bool:
    detail = str(exc.detail or "").lower()
    if "checkout promotion update failed" in detail or "checkout taxes update failed" in detail:
        return is_retryable_chatgpt_http_exception(exc)
    retryable_markers = (
        "payment_method_types_mismatch",
        "stripe submission failed",
        "generic_decline",
        "checkout_confirm_error",
        "submission_attempt",
    )
    if any(marker in detail for marker in retryable_markers):
        return True
    return is_retryable_provider_http_exception(exc)


def provider_stage_proxy(req: LongLinkRequest) -> str:
    if provider_uses_checkout(req):
        return checkout_stage_proxy(req)
    override = str(getattr(req, "provider_proxy_override", "") or "").strip()
    if override:
        return normalize_proxy_url(override)
    country = provider_stage_country(req)
    if single_proxy_mode(req) or country == "SAME":
        return checkout_stage_proxy(req)
    if country == "AU":
        return normalize_proxy_url(str(req.au_proxy or "").strip())
    explicit = str(req.us_proxy or "").strip()
    if explicit:
        return normalize_proxy_url(explicit)
    return normalize_proxy_url(PROVIDER_STAGE_PROXY)


def apply_provider_proxy(chatgpt: Any, proxy: str, parent_proxy: str = "") -> None:
    set_proxy_url(chatgpt, proxy, parent_proxy)


def fetch_proxy_geo(session: Any) -> tuple[str, str, str]:
    # 711 rotating residential proxies are sensitive to simultaneous auth tunnels.
    # Probe sequentially and treat per-provider 429/407 as a probe-source failure,
    # not as proof that the proxy itself is dead.
    probes = [
        (
            "ipwho.is",
            "https://ipwho.is/",
            lambda payload: (
                str(payload.get("ip") or ""),
                str(payload.get("country_code") or "").upper(),
                str(payload.get("country") or ""),
                str(payload.get("message") or ""),
                bool(payload.get("success", True)),
            ),
        ),
        (
            "ipapi.co",
            "https://ipapi.co/json/",
            lambda payload: (
                str(payload.get("ip") or ""),
                str(payload.get("country_code") or "").upper(),
                str(payload.get("country_name") or ""),
                str(payload.get("reason") or payload.get("error") or ""),
                not bool(payload.get("error")),
            ),
        ),
        (
            "ip-api",
            "http://ip-api.com/json/?fields=status,message,country,countryCode,query",
            lambda payload: (
                str(payload.get("query") or ""),
                str(payload.get("countryCode") or "").upper(),
                str(payload.get("country") or ""),
                str(payload.get("message") or ""),
                str(payload.get("status") or "") == "success",
            ),
        ),
    ]

    proxy_config = dict(getattr(session, "proxies", {}) or {})
    header_config = dict(getattr(session, "headers", {}) or {})
    errors: list[str] = []

    for round_index in range(2):
        for name, url, parser in probes:
            probe_session = new_session()
            if hasattr(probe_session, "trust_env"):
                probe_session.trust_env = False
            probe_session.proxies = dict(proxy_config)
            if hasattr(probe_session, "headers"):
                probe_session.headers.update(header_config)
            try:
                response = probe_session.get(url, timeout=10)
                if response.status_code in (407, 429):
                    errors.append(f"{name}: HTTP {response.status_code}: {response.text[:120]}")
                    continue
                response.raise_for_status()
                ip, country_code, country, message, ok = parser(response.json() or {})
                if ok and ip and country_code:
                    return ip, country_code, country
                errors.append(f"{name}: {message or response.text[:120]}")
            except Exception as exc:
                errors.append(f"{name}: {exc}")
            finally:
                try:
                    probe_session.close()
                except Exception:
                    pass
        if round_index == 0:
            time.sleep(1.0)

    raise RuntimeError("；".join(errors[-8:]))

def probe_proxy(label: str, proxy: str, expected_country: str = "", required: bool = False, parent_proxy: str = "") -> ProxyProbeResult:
    proxy = normalize_proxy_url(proxy)
    expected_country = str(expected_country or "").strip().upper()
    if not proxy:
        if required:
            return ProxyProbeResult(ok=False, label=label, proxy="", error=f"{label} 代理不能为空")
        return ProxyProbeResult(ok=True, label=label, proxy="", error=f"{label} 未填写，使用直连")

    parsed = urlsplit(proxy)
    username = unquote(parsed.username or "")
    if username.startswith("-"):
        return ProxyProbeResult(
            ok=False,
            label=label,
            proxy=proxy,
            error=f"{label} 代理用户名不应以 '-' 开头，请使用 Kookeey 页面生成的原始格式",
        )

    session = new_session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    set_proxy_url(session, proxy, parent_proxy)
    try:
        ip, country_code, country = fetch_proxy_geo(session)
    except Exception as exc:
        return ProxyProbeResult(ok=False, label=label, proxy=proxy, error=f"{label} 检测失败: {exc}")
    result = ProxyProbeResult(
        ok=True,
        label=label,
        proxy=proxy,
        ip=ip,
        country=country,
        country_code=country_code,
    )
    if expected_country and country_code != expected_country:
        result.ok = False
        result.error = f"{label} 出口不是 {expected_country}: {country_code or '未知'} {result.ip}".strip()
    return result


def check_request_proxies(req: LongLinkRequest) -> list[ProxyProbeResult]:
    checkout_proxy = checkout_stage_proxy(req)
    provider_proxy = provider_stage_proxy(req)
    provider_country = provider_stage_country(req)
    checkout_country = checkout_stage_country(req)
    approve_proxy = approve_stage_proxy(req)
    approve_country = approve_stage_country(req)
    promotion_proxy = promotion_stage_proxy(req)
    promotion_country = promotion_stage_country(req)
    parent_proxy = chain_parent_proxy(req)
    if single_proxy_mode(req):
        results = [probe_proxy("统一代理", checkout_proxy, "", True, parent_proxy)]
    else:
        results = [probe_proxy(f"checkout {checkout_country}", checkout_proxy, checkout_country, True, parent_proxy)]
        if provider_proxy != checkout_proxy:
            results.append(probe_proxy(f"provider {provider_country}", provider_proxy, provider_country, True, parent_proxy))
        if approve_proxy != checkout_proxy:
            results.append(probe_proxy(f"approve {approve_country}", approve_proxy, approve_country, True, parent_proxy))
        if promotion_proxy:
            results.append(probe_proxy(f"promotion/update/poll {promotion_country}", promotion_proxy, promotion_country, True, parent_proxy))
    mark_same_exit(results)
    return results


def check_provider_proxy(req: LongLinkRequest) -> ProxyProbeResult:
    country = provider_stage_country(req)
    expected_country = "" if single_proxy_mode(req) else country
    return probe_proxy(provider_stage_label(req), provider_stage_proxy(req), expected_country, required=True, parent_proxy=chain_parent_proxy(req))


def checkout_retry_limit(req: Any) -> int:
    try:
        value = int(getattr(req, "checkout_retry_limit", CHATGPT_RETRY_ATTEMPTS) or CHATGPT_RETRY_ATTEMPTS)
    except Exception:
        value = CHATGPT_RETRY_ATTEMPTS
    return max(1, min(50, value))


def mark_same_exit(results: list[ProxyProbeResult]) -> None:
    if len(results) < 2:
        return
    first = results[0]
    first_ip = str(first.ip or "").strip()
    if not first_ip:
        return
    for item in results[1:]:
        if str(item.ip or "").strip() == first_ip and item.label != first.label:
            item.ok = False
            suffix = f"；并且与 {first.label} 使用同一出口 {first_ip}"
            item.error = (item.error or f"{item.label} 出口异常").rstrip("。") + suffix


def ensure_request_proxies_ok(req: LongLinkRequest) -> list[ProxyProbeResult]:
    results = check_request_proxies(req)
    failed = [item for item in results if not item.ok]
    if failed:
        detail = "; ".join(item.error or f"{item.label} 代理不可用" for item in failed)
        raise HTTPException(status_code=400, detail=f"代理检测未通过: {detail}")
    return results


def format_proxy_probe_summary(results: list[ProxyProbeResult]) -> str:
    parts: list[str] = []
    for item in results:
        if not item.proxy:
            parts.append(f"{item.label}: 直连")
            continue
        if item.ok:
            summary = f"{item.label}: {item.ip or '未知 IP'} / {item.country_code or '未知'} {item.country or ''}".strip()
        else:
            summary = f"{item.label}: {item.error or '检测失败'}"
        parts.append(summary)
    return "；".join(parts)


def currency_for_country(country: str) -> str:
    return COUNTRY_CURRENCY.get(str(country or "").upper(), "USD")


def normalize_country(country: str) -> str:
    country = str(country or "").strip().upper()
    if not country:
        return "US"
    if country in OPENAI_SUPPORTED_COUNTRY_CODES:
        return country
    raise HTTPException(status_code=400, detail=f"不支持的账单资料地区: {country}")


def effective_country(req: LongLinkRequest) -> str:
    return normalize_country(req.billing_country)


def effective_payment_method_country(req: LongLinkRequest) -> str:
    return normalize_country(req.payment_method_country or "US")


def effective_promotion_country(req: LongLinkRequest) -> str:
    return normalize_country(getattr(req, "promotion_country", "") or getattr(req, "batch_checkout_proxy_country", "") or "TR")


def payment_method_country_pool(req: LongLinkRequest) -> list[str]:
    raw = getattr(req, "payment_method_country_pool", []) or []
    if isinstance(raw, str):
        items = re.split(r"[,;\s|]+", raw)
    else:
        items = list(raw)

    countries: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        country = normalize_country(text)
        if country in seen:
            continue
        seen.add(country)
        countries.append(country)

    return countries or [effective_payment_method_country(req)]


def locale_parts(locale: str) -> tuple[str, str]:
    return LOCALE_MAP.get(str(locale or "").strip(), LOCALE_MAP["en"])


def find_token(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("accessToken", "access_token", "token"):
            token = str(value.get(key) or "").strip()
            if token:
                return token
        for item in value.values():
            token = find_token(item)
            if token:
                return token
    if isinstance(value, list):
        for item in value:
            token = find_token(item)
            if token:
                return token
    return ""


def normalize_access_token(raw: str) -> str:
    token = str(raw or "").strip()
    if not token:
        return ""
    if token.startswith("{") or token.startswith("["):
        try:
            return find_token(json.loads(token)) or token
        except json.JSONDecodeError:
            return token
    return token


def extract_processor_entity(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    direct = data.get("processor_entity") or data.get("processorEntity")
    if direct:
        return str(direct).strip()
    for key in ("checkout_session", "session", "checkout", "data"):
        nested = data.get(key)
        if isinstance(nested, dict):
            found = extract_processor_entity(nested)
            if found:
                return found
    return ""


def extract_stripe_publishable_key(data: Any) -> str:
    if isinstance(data, str):
        match = re.search(r"pk_live_[A-Za-z0-9]+", data)
        return match.group(0) if match else ""
    if isinstance(data, dict):
        for key in (
            "stripe_publishable_key",
            "publishable_key",
            "publishableKey",
            "stripePublishableKey",
            "key",
        ):
            found = extract_stripe_publishable_key(data.get(key))
            if found:
                return found
        for item in data.values():
            found = extract_stripe_publishable_key(item)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = extract_stripe_publishable_key(item)
            if found:
                return found
    return ""


def build_chatgpt_session(
    req: LongLinkRequest,
    proxy_override: str = "",
    source_session: Any | None = None,
) -> Any:
    access_token = normalize_access_token(req.access_token)
    if not access_token:
        raise HTTPException(status_code=400, detail="accessToken is required")

    device_id = req.device_id.strip() or str(uuid.uuid4())
    user_agent = req.user_agent.strip() or DEFAULT_USER_AGENT
    session = new_session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Authorization": f"Bearer {access_token}",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "Content-Type": "application/json",
            "oai-device-id": device_id,
            "oai-language": "en-US",
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "Cookie": f"oai-did={device_id}",
        }
    )
    source_headers = getattr(source_session, "headers", None)
    if source_headers:
        session.headers.update(dict(source_headers))
    source_cookies = getattr(source_session, "cookies", None)
    target_cookies = getattr(session, "cookies", None)
    if source_cookies is not None and target_cookies is not None:
        try:
            target_cookies.update(source_cookies)
        except Exception:
            pass
    set_proxy_url(session, proxy_override or checkout_stage_proxy(req), chain_parent_proxy(req))
    return session


def create_checkout(req: LongLinkRequest, chatgpt_session: Any | None = None) -> dict[str, Any]:
    billing_country = effective_country(req)
    currency = currency_for_country(billing_country)
    checkout_ui_mode = (req.checkout_ui_mode or "custom").strip() or "custom"
    body = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {
            "country": billing_country,
            "currency": currency,
        },
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": checkout_ui_mode,
    }
    headers = {
        "Referer": "https://chatgpt.com/",
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
    }
    response = (chatgpt_session or build_chatgpt_session(req)).post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json=body,
        headers=headers,
        timeout=CHATGPT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=http_error_detail("checkout create failed", response),
        )

    data = response.json() or {}
    cs_id = data.get("checkout_session_id") or data.get("session_id") or data.get("id")
    if not cs_id or not str(cs_id).startswith("cs_"):
        raise HTTPException(status_code=502, detail=f"checkout response missing cs_id: {data}")
    return {
        "cs_id": str(cs_id),
        "processor_entity": extract_processor_entity(data),
        "stripe_publishable_key": extract_stripe_publishable_key(data),
        "billing_country": billing_country,
        "currency": currency,
    }


def create_checkout_with_retry(req: LongLinkRequest, emit: Any | None = None) -> tuple[LongLinkRequest, Any, dict[str, Any]]:
    last_error = ""
    max_attempts = checkout_retry_limit(req)
    used_checkout_bases: set[str] = set()
    for attempt in range(1, max_attempts + 1):
        ensure_not_cancelled()
        country = checkout_stage_country(req)
        attempt_req = request_with_rotated_checkout_session(req, used_bases=used_checkout_bases)
        probe_label = "统一代理" if single_proxy_mode(attempt_req) else f"checkout {country}"
        expected_country = "" if single_proxy_mode(attempt_req) else country
        if emit:
            emit("checkout", f"checkout 第 {attempt}/{max_attempts} 次：正在检测 {probe_label}。")
        probe = probe_proxy(probe_label, checkout_stage_proxy(attempt_req), expected_country, required=True, parent_proxy=chain_parent_proxy(attempt_req))
        if not probe.ok:
            last_error = probe.error or f"{probe_label} 检测失败"
            if emit:
                emit("checkout", f"checkout 第 {attempt} 次 {probe_label} 不可用：{last_error}")
            continue
        if emit:
            emit("checkout", f"{probe_label} 出口：{probe.ip} / {probe.country_code} {probe.country}。")
        try:
            if emit:
                emit("checkout", f"checkout 第 {attempt}/{max_attempts} 次：正在创建 ChatGPT checkout。")
            chatgpt = build_chatgpt_session(attempt_req)
            checkout = create_checkout(attempt_req, chatgpt)
            checkout["checkout_proxy_ip"] = probe.ip
            checkout["checkout_proxy_country_code"] = probe.country_code
            return attempt_req, chatgpt, checkout
        except HTTPException as exc:
            if not is_retryable_chatgpt_http_exception(exc):
                raise
            last_error = str(exc.detail or exc)
            if emit:
                emit("checkout", f"checkout 第 {attempt} 次返回 HTML/风控/限流，正在更换 {country} session：{short_error(last_error)}")
                if attempt < max_attempts:
                    emit("checkout", f"checkout 可重试错误，{PROXY_EXIT_RETRY_DELAY_SECONDS}秒后重试 {attempt + 1}/{max_attempts}。")
            if attempt < max_attempts:
                cancellable_sleep(PROXY_EXIT_RETRY_DELAY_SECONDS)
        except Exception as exc:
            if not is_retryable_network_error(exc):
                raise
            last_error = str(exc)
            if emit:
                emit("checkout", f"checkout 第 {attempt} 次网络超时/连接失败，正在更换 {country} session：{last_error}")
            if attempt < max_attempts:
                cancellable_sleep(PROXY_EXIT_RETRY_DELAY_SECONDS)
    raise HTTPException(
        status_code=504,
        detail=f"ChatGPT checkout 连续失败，已自动更换 session {max_attempts} 次仍失败: {last_error}",
    )


def stripe_key_for_request(req: LongLinkRequest, checkout: dict[str, Any] | None = None) -> str:
    return (
        req.stripe_publishable_key.strip()
        or str((checkout or {}).get("stripe_publishable_key") or "").strip()
        or DEFAULT_STRIPE_PK
    )


def stripe_init(
    cs_id: str,
    req: LongLinkRequest,
    proxy_override: str = "",
    checkout: dict[str, Any] | None = None,
    stripe_session: Any | None = None,
) -> dict[str, Any]:
    stripe_pk = stripe_key_for_request(req, checkout)
    browser_locale, elements_locale = locale_parts(req.payment_locale)
    stripe_js_id = str(uuid.uuid4())
    get_params = {
        "key": stripe_pk,
        "eid": "NA",
        "browser_locale": browser_locale,
        "elements_locale": elements_locale,
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": elements_locale,
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "_stripe_version": STRIPE_VERSION_REFERENCE,
    }
    stripe = stripe_session or new_session()
    stripe.headers.update(
        {
            "User-Agent": req.user_agent.strip() or DEFAULT_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    if proxy_override:
        set_proxy_url(stripe, proxy_override, chain_parent_proxy(req))
    else:
        set_proxy_url(stripe, checkout_stage_proxy(req), chain_parent_proxy(req))
    body = {
        "browser_locale": browser_locale,
        "browser_timezone": "Asia/Shanghai",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": elements_locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    url = f"https://api.stripe.com/v1/payment_pages/{cs_id}/init"
    response = stripe.get(url, params=get_params, timeout=DEFAULT_TIMEOUT)
    if response.status_code >= 400:
        fallback_params = dict(get_params)
        fallback_params.pop("eid", None)
        fallback_params.pop("elements_locale", None)
        fallback_params["_stripe_version"] = STRIPE_VERSION_FULL
        response = stripe.get(url, params=fallback_params, timeout=DEFAULT_TIMEOUT)
    if response.status_code >= 400:
        response = stripe.post(url, data=body, timeout=DEFAULT_TIMEOUT)
    if response.status_code >= 400:
        detail = response.text[:500]
        try:
            error = (response.json() or {}).get("error") or {}
            if error.get("code") == "resource_missing":
                detail = (
                    f"{detail}；诊断：Stripe 找不到该 checkout.session，通常是 checkout_ui_mode "
                    f"不是 custom，或 Stripe publishable key 与 checkout session 所属账户不匹配。"
                )
        except Exception:
            pass
        raise HTTPException(
            status_code=response.status_code,
            detail=f"stripe init failed: {detail}",
        )
    payload = response.json() or {}
    if isinstance(payload, dict) and not payload.get("stripe_hosted_url"):
        payload["stripe_hosted_url"] = stripe_checkout_long_url(
            cs_id,
            str((checkout or {}).get("billing_country") or "US"),
            str((checkout or {}).get("processor_entity") or ""),
        )
    return payload


def to_openai_pay_url(stripe_hosted_url: str) -> str:
    url = str(stripe_hosted_url or "").strip()
    if not url:
        return ""
    if url.startswith("https://checkout.stripe.com"):
        return "https://pay.openai.com" + url[len("https://checkout.stripe.com") :]

    parsed = urlsplit(url)
    if parsed.netloc.lower() == "checkout.stripe.com":
        return urlunsplit((parsed.scheme or "https", "pay.openai.com", parsed.path, parsed.query, parsed.fragment))
    return url


def processor_entity_for_country(country: str, processor_entity: str = "") -> str:
    entity = str(processor_entity or "").strip()
    if entity:
        return entity
    return "openai_llc" if str(country or "").upper() == "US" else "openai_ie"


def chatgpt_success_return_url(cs_id: str, country: str, processor_entity: str = "") -> str:
    entity = processor_entity_for_country(country, processor_entity)
    return f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}&processor_entity={entity}&plan_type=plus"


def stripe_checkout_long_url(cs_id: str, country: str, processor_entity: str = "") -> str:
    return (
        f"https://checkout.stripe.com/c/pay/{cs_id}"
        f"?returned_from_redirect=true&ui_mode=custom&return_url="
        f"{quote(chatgpt_success_return_url(cs_id, country, processor_entity), safe='')}"
    )


def stripe_confirm_return_url(cs_id: str, checkout: dict[str, Any], stripe_hosted_url: str) -> str:
    hosted_url = to_openai_pay_url(stripe_hosted_url) or stripe_checkout_long_url(
        cs_id,
        checkout["billing_country"],
        checkout.get("processor_entity", ""),
    )
    if "pay.openai.com/" in hosted_url or "checkout.stripe.com/" in hosted_url:
        parsed = urlsplit(hosted_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault(
            "success_return_url",
            chatgpt_success_return_url(
                cs_id,
                checkout["billing_country"],
                checkout.get("processor_entity", ""),
            ),
        )
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    return hosted_url


def summarize_payment_method_hints(payload: Any, limit: int = 24) -> str:
    hints: list[str] = []
    seen: set[str] = set()
    interesting_keys = (
        "payment_method_types",
        "ordered_payment_method_types",
        "visible_payment_method_types",
        "payment_method_type",
        "payment_method",
        "type",
    )

    def add(value: Any, path: str) -> None:
        text = str(value).strip()
        if not text:
            return
        item = f"{path}={text[:120]}"
        if item in seen:
            return
        seen.add(item)
        hints.append(item)

    def walk(value: Any, path: str = "root", depth: int = 0) -> None:
        if len(hints) >= limit or depth > 6:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key)
                next_path = f"{path}.{key_text}" if path else key_text
                lowered = key_text.lower()
                if any(token in lowered for token in interesting_keys):
                    if isinstance(item, (str, int, float, bool)):
                        add(item, next_path)
                    elif isinstance(item, list):
                        add(",".join(str(part) for part in item[:20]), next_path)
                    elif isinstance(item, dict):
                        direct = item.get("type") or item.get("id") or item.get("name")
                        if direct:
                            add(direct, next_path)
                walk(item, next_path, depth + 1)
        elif isinstance(value, list):
            for index, item in enumerate(value[:40]):
                walk(item, f"{path}[{index}]", depth + 1)

    walk(payload)
    return "；".join(hints[:limit]) or "未在 init payload 中找到支付方式字段"


def init_payload_has_paypal_hint(payload: Any) -> bool:
    return "paypal" in summarize_payment_method_hints(payload, limit=80).lower()


def expected_amount(init_payload: Any) -> str:
    return stripe_amount_info(init_payload)[0]


def stripe_amount_info(init_payload: Any) -> tuple[str, str]:
    if not isinstance(init_payload, dict):
        return "", "missing_payload"
    total_summary = init_payload.get("total_summary")
    if isinstance(total_summary, dict) and total_summary.get("due") is not None:
        return str(total_summary.get("due")), "total_summary.due"
    invoice = init_payload.get("invoice")
    if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
        return str(invoice.get("amount_due")), "invoice.amount_due"
    line_items = init_payload.get("line_items")
    if isinstance(line_items, list):
        total = 0
        found = False
        for item in line_items:
            if isinstance(item, dict) and item.get("amount") is not None:
                try:
                    total += int(item.get("amount") or 0)
                    found = True
                except Exception:
                    pass
        if found:
            return str(total), "line_items.amount"
    return "", "missing_amount"


def is_unknown_amount(amount: Any) -> bool:
    return not str(amount if amount is not None else "").strip()


def is_zero_amount(amount: Any) -> bool:
    text = str(amount if amount is not None else "").strip()
    if not text:
        return False
    try:
        return float(text) == 0
    except Exception:
        return text in {"0", "0.0", "0.00"}


def stripe_context(cs_id: str, init_payload: dict[str, Any], req: LongLinkRequest) -> dict[str, Any]:
    _, elements_locale = locale_parts(req.payment_locale)
    return {
        "stripe_js_id": str(uuid.uuid4()),
        "elements_session_id": f"elements_session_{uuid.uuid4().hex[:11]}",
        "elements_session_config_id": str(init_payload.get("config_id") or uuid.uuid4()),
        "config_id": init_payload.get("config_id") or "",
        "init_checksum": init_payload.get("init_checksum") or "",
        "currency": str(init_payload.get("currency") or currency_for_country(effective_country(req))).lower(),
        "checkout_amount": expected_amount(init_payload),
        "locale": elements_locale,
        "runtime_version": DEFAULT_STRIPE_RUNTIME_VERSION,
    }


def random_postal_code(pattern: str) -> str:
    result = []
    for char in str(pattern or "#####"):
        if char == "#":
            result.append(str(random.randint(0, 9)))
        elif char == "A":
            result.append(chr(random.randint(ord("A"), ord("Z"))))
        else:
            result.append(char)
    return "".join(result)


def billing_profile_source(country: str) -> str:
    profile = BILLING_PROFILE_BY_COUNTRY.get(normalize_country(country), {})
    return str(profile.get("source") or "formatted")


def billing_for_country(country: str) -> dict[str, str]:
    country = normalize_country(country)
    if country == "DE":
        first_name, last_name = random.choice(DE_BILLING_NAMES)
        line1, city, state, postal_code = random.choice(DE_BILLING_STREETS)
    elif country == "AU":
        first_name, last_name = random.choice(AU_BILLING_NAMES)
        line1, city, state, postal_code = random.choice(AU_BILLING_STREETS)
    elif country == "US":
        first_name, last_name = random.choice(US_BILLING_NAMES)
        line1, city, state, postal_code = random.choice(US_BILLING_STREETS)
    elif country in EXTRA_BILLING_STREETS:
        first_name, last_name = random.choice(EXTRA_BILLING_NAMES)
        line1, city, state, postal_code = random.choice(EXTRA_BILLING_STREETS[country])
    elif country in OPENAI_SUPPORTED_COUNTRY_CODES:
        profile = BILLING_PROFILE_BY_COUNTRY[country]
        first_name, last_name = random.choice(EXTRA_BILLING_NAMES)
        line1 = f"{random.randint(10, 999)} {random.choice(profile['street_pool'])}"
        city = random.choice(profile["city_pool"])
        state = country
        postal_code = random_postal_code(str(profile.get("postal_pattern") or "#####"))
    else:
        raise HTTPException(status_code=400, detail=f"不支持的账单资料地区: {country}")
    suffix = random.randint(1000, 9999)
    profile = BILLING_PROFILE_BY_COUNTRY.get(country, {})
    phone_prefix = str(profile.get("phone_prefix") or COUNTRY_PHONE_PREFIX.get(country, "+1"))
    phone = f"{phone_prefix}{random.randint(100000000, 999999999)}"
    return {
        "name": f"{first_name} {last_name}",
        "email": f"{first_name.lower()}.{last_name.lower()}{suffix}@example.com",
        "phone": phone,
        "country": country,
        "line1": line1,
        "city": city,
        "state": state,
        "postal_code": postal_code,
    }


def build_stripe_session(req: LongLinkRequest, proxy_override: str = "") -> Any:
    stripe = new_session()
    stripe.headers.update(
        {
            "User-Agent": req.user_agent.strip() or DEFAULT_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    if proxy_override:
        set_proxy_url(stripe, proxy_override, chain_parent_proxy(req))
    else:
        set_proxy_url(stripe, checkout_stage_proxy(req), chain_parent_proxy(req))
    return stripe


def post_chatgpt_checkout_action(
    chatgpt: Any,
    cs_id: str,
    path: str,
    payload: dict[str, Any],
    label: str,
    checkout: dict[str, Any],
) -> dict[str, Any]:
    processor_entity = processor_entity_for_country(
        str(checkout.get("billing_country") or "US"),
        str(checkout.get("processor_entity") or ""),
    )
    response = chatgpt.post(
        "https://chatgpt.com" + path,
        json=payload,
        headers={
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}",
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        },
        timeout=CHATGPT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=http_error_detail(f"{label} failed via {path}", response),
        )
    try:
        return response.json() or {}
    except Exception:
        return {}


def update_checkout_promotion(
    chatgpt: Any,
    cs_id: str,
    checkout: dict[str, Any],
    emit: Any | None = None,
) -> dict[str, Any]:
    payload = {
        "checkout_session_id": cs_id,
        "processor_entity": processor_entity_for_country(
            str(checkout.get("billing_country") or "US"),
            str(checkout.get("processor_entity") or ""),
        ),
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
    }
    if emit:
        emit("sync", "正在通过右池同步 plus-1-month-free 优惠。")
    result = post_chatgpt_checkout_action(
        chatgpt,
        cs_id,
        "/backend-api/payments/checkout/update",
        payload,
        "checkout promotion update",
        checkout,
    )
    if emit:
        emit("sync", "plus-1-month-free 优惠更新成功。")
    return result


def update_checkout_taxes(
    chatgpt: Any,
    cs_id: str,
    checkout: dict[str, Any],
    billing: dict[str, str],
    emit: Any | None = None,
) -> dict[str, Any]:
    billing_country = str(billing.get("country") or checkout.get("billing_country") or "US").upper()
    billing_address = {
        "country": billing_country,
        "line1": billing.get("line1") or "",
        "city": billing.get("city") or "",
        "postal_code": billing.get("postal_code") or "",
        "state": billing.get("state") or "",
    }
    if billing.get("line2"):
        billing_address["line2"] = billing["line2"]
    payload = {
        "checkout_session_id": cs_id,
        "checkout_email": billing.get("email") or "buyer@example.com",
        "billing_country": billing_country,
        "billing_name": billing.get("name") or None,
        "currency": str(checkout.get("currency") or currency_for_country(billing_country)).lower(),
        "tax_id": None,
        "processor_entity": processor_entity_for_country(
            str(checkout.get("billing_country") or billing_country),
            str(checkout.get("processor_entity") or ""),
        ),
        "billing_address": billing_address,
    }
    if emit:
        emit("sync", f"正在通过右池同步 {billing_country} 账单与税区。")
    result = post_chatgpt_checkout_action(
        chatgpt,
        cs_id,
        "/backend-api/payments/checkout/taxes",
        payload,
        "checkout taxes update",
        checkout,
    )
    if emit:
        emit("sync", f"{billing_country} 账单与税区同步成功。")
    return result


def prepare_paypal_after_sync(
    chatgpt: Any,
    checkout: dict[str, Any],
    req: LongLinkRequest,
    provider_proxy: str = "",
    promotion_proxy: str = "",
    emit: Any | None = None,
) -> tuple[dict[str, Any], str, dict[str, str]]:
    cs_id = checkout["cs_id"]
    pm_country = effective_payment_method_country(req)
    billing = billing_for_country(pm_country)
    provider_proxy = normalize_proxy_url(provider_proxy or provider_stage_proxy(req))
    promotion_proxy = normalize_proxy_url(promotion_proxy or promotion_stage_proxy(req))
    checkout["promotion_country"] = promotion_stage_country(req)
    if emit:
        emit(
            "sync",
            f"Step 3：右池={promotion_stage_country(req)}，同步 plus-1-month-free 与 {billing['country']} 税区。",
        )
    promotion_chatgpt = build_chatgpt_session(
        req,
        proxy_override=promotion_proxy,
        source_session=chatgpt,
    )
    update_checkout_promotion(promotion_chatgpt, cs_id, checkout, emit=emit)
    update_checkout_taxes(promotion_chatgpt, cs_id, checkout, billing, emit=emit)
    stripe = build_stripe_session(req, proxy_override=provider_proxy)
    if emit:
        emit("stripe_init", "Step 4：使用左池 provider 出口执行 Stripe init。")
    init_payload = stripe_init(
        cs_id,
        req,
        proxy_override=provider_proxy,
        checkout=checkout,
        stripe_session=stripe,
    )
    hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not hosted_url:
        hosted_url = stripe_checkout_long_url(
            cs_id,
            str(checkout.get("billing_country") or "US"),
            str(checkout.get("processor_entity") or ""),
        )
        init_payload["stripe_hosted_url"] = hosted_url
    if emit:
        amount, amount_source = stripe_amount_info(init_payload)
        emit(
            "stripe_amount",
            f"Stripe 金额：amount={amount or 'unknown'} source={amount_source}。",
            stripe_amount=amount,
            stripe_amount_source=amount_source,
        )
    return init_payload, hosted_url, billing


def stripe_create_payment_method(
    stripe: Any,
    cs_id: str,
    stripe_pk: str,
    billing: dict[str, str],
    ctx: dict[str, Any],
) -> str:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    body = {
        "billing_details[name]": billing.get("name") or "John Doe",
        "billing_details[email]": billing.get("email") or "buyer@example.com",
        "billing_details[phone]": billing.get("phone") or "",
        "billing_details[address][country]": billing.get("country") or "US",
        "billing_details[address][line1]": billing.get("line1") or "3110 Sunset Boulevard",
        "billing_details[address][city]": billing.get("city") or "Los Angeles",
        "billing_details[address][postal_code]": billing.get("postal_code") or "90026",
        "billing_details[address][state]": billing.get("state") or "CA",
        "type": "paypal",
        "payment_user_agent": f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    response = stripe.post("https://api.stripe.com/v1/payment_methods", data=body, timeout=DEFAULT_TIMEOUT)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=f"stripe payment_methods failed: {response.text[:500]}")
    pm_id = str((response.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise HTTPException(status_code=502, detail=f"stripe payment_methods bad response: {response.text[:300]}")
    return pm_id


def stripe_error_summary(prefix: str, response: Any) -> str:
    try:
        payload = response.json() or {}
    except Exception:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        error = {}
    extra_fields = error.get("extra_fields") if isinstance(error.get("extra_fields"), dict) else {}
    parts: list[str] = []
    for label, value in (
        ("code", error.get("code")),
        ("decline_code", error.get("decline_code")),
        ("type", error.get("type")),
        ("message", error.get("message")),
        ("payment_method_type", extra_fields.get("payment_method_type")),
        ("confirm_error_reason", extra_fields.get("confirm_error_reason")),
        ("confirm_error_code", extra_fields.get("confirm_error_code")),
        ("confirm_error_message", extra_fields.get("confirm_error_message")),
    ):
        if value is None or value == "":
            continue
        parts.append(f"{label}={short_error(str(value), 180)}")
    if parts:
        return f"{prefix}: " + ", ".join(parts)
    return f"{prefix}: {short_error(response.text, 500)}"


def stripe_confirm(
    stripe: Any,
    cs_id: str,
    pm_id: str,
    stripe_pk: str,
    init_payload: dict[str, Any],
    ctx: dict[str, Any],
    checkout: dict[str, Any],
    req: LongLinkRequest,
    stripe_hosted_url: str,
) -> dict[str, Any]:
    return_url = stripe_confirm_return_url(cs_id, checkout, stripe_hosted_url)
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    body = {
        "guid": uuid.uuid4().hex,
        "muid": uuid.uuid4().hex,
        "sid": uuid.uuid4().hex,
        "payment_method": pm_id,
        "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
        "version": runtime_version,
        "expected_amount": str(ctx.get("checkout_amount") or expected_amount(init_payload)),
        "expected_payment_method_type": "paypal",
        "return_url": return_url,
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[locale]": str(ctx.get("locale") or "en"),
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "consent[terms_of_service]": "accepted",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    response = stripe.post(f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm", data=body, timeout=DEFAULT_TIMEOUT)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=stripe_error_summary("stripe confirm failed", response))
    return response.json() or {}


def is_external_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def is_paypal_url(value: str) -> bool:
    host = (urlsplit(value).netloc or "").lower()
    return host == "paypal.com" or host.endswith(".paypal.com") or host == "paypalobjects.com" or host.endswith(".paypalobjects.com")


def is_paypal_ba_approve_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    if not (host == "paypal.com" or host.endswith(".paypal.com")):
        return False
    path = parsed.path.rstrip("/").lower()
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return path == "/agreements/approve" and bool(str(query.get("ba_token") or "").strip())


def is_stripe_pm_redirect_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and (parsed.hostname or "").lower() == "pm-redirects.stripe.com"


def is_ignored_resource_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    ignored_hosts = {
        "stripe-camo.global.ssl.fastly.net",
        "files.stripe.com",
        "q.stripe.com",
        "js.stripe.com",
        "m.stripe.network",
    }
    ignored_suffixes = (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ico", ".css", ".js", ".woff", ".woff2")
    if host in ignored_hosts or any(host.endswith(f".{item}") for item in ignored_hosts):
        return True
    if path.endswith(ignored_suffixes):
        return True
    return False


def extract_browser_redirect_url(base_url: str, text: str) -> str:
    body = html.unescape(str(text or ""))
    urls = collect_urls(body)
    for url in urls:
        if is_paypal_ba_approve_url(url):
            return url
    for url in urls:
        if is_paypal_url(url) and not is_ignored_resource_url(url):
            return url
    meta = re.search(
        r"<meta[^>]+http-equiv=[\"']?refresh[\"']?[^>]+content=[\"'][^\"']*url=([^\"'>\s]+)",
        body,
        flags=re.IGNORECASE,
    )
    if meta:
        return urljoin(base_url, meta.group(1).strip())
    js = re.search(
        r"(?:window\.)?location(?:\.href)?\s*=\s*[\"']([^\"']+)[\"']",
        body,
        flags=re.IGNORECASE,
    )
    if js:
        return urljoin(base_url, js.group(1).strip())
    for url in urls:
        if is_external_url(url) and not is_ignored_resource_url(url):
            return url
    return ""


def is_non_return_external_url(value: str) -> bool:
    host = (urlsplit(value).netloc or "").lower()
    return (
        is_external_url(value)
        and not is_ignored_resource_url(value)
        and host not in {"chatgpt.com", "pay.openai.com"}
        and not host.endswith(".chatgpt.com")
    )


def collect_urls(payload: Any, urls: list[str] | None = None) -> list[str]:
    found = urls if urls is not None else []
    if isinstance(payload, str):
        for match in re.findall(r"https?://[^\s\"'<>]+", payload):
            found.append(match.rstrip("),.;]"))
    elif isinstance(payload, dict):
        for key, value in payload.items():
            if key in ("url", "return_url", "redirect_url", "redirect_to_url") and isinstance(value, str) and is_external_url(value):
                found.append(value)
            else:
                collect_urls(value, found)
    elif isinstance(payload, list):
        for item in payload:
            collect_urls(item, found)
    return found


def extract_redirect_to_url(payload: Any) -> str:
    if not isinstance(payload, dict):
        urls = collect_urls(payload)
        return next(
            (item for item in urls if is_paypal_ba_approve_url(item)),
            next((item for item in urls if is_paypal_url(item) and not is_ignored_resource_url(item)), ""),
        )
    next_action = payload.get("next_action")
    if isinstance(next_action, dict) and next_action.get("type") == "redirect_to_url":
        redirect_to_url = next_action.get("redirect_to_url") or {}
        if isinstance(redirect_to_url, dict):
            url = str(redirect_to_url.get("url") or "").strip()
            if url:
                return url
    for key in ("setup_intent", "payment_intent"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = extract_redirect_to_url(nested)
            if found:
                return found
    urls = collect_urls(payload)
    return next(
        (item for item in urls if is_paypal_ba_approve_url(item)),
        next((item for item in urls if is_paypal_url(item) and not is_ignored_resource_url(item)), ""),
    )


def stripe_payload_diagnostics(payload: Any, ctx: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return f"payload_type={type(payload).__name__}"
    keys = ",".join(sorted(payload.keys())[:12])
    urls = collect_urls(payload)
    paypal_count = sum(1 for item in urls if is_paypal_url(item))
    ba_count = sum(1 for item in urls if is_paypal_ba_approve_url(item))
    ignored_count = sum(1 for item in urls if is_ignored_resource_url(item))
    submission = find_submission_attempt(payload)
    submission_state = str(submission.get("state") or "") if isinstance(submission, dict) else ""
    submission_fields = submission_attempt_failure_fields(submission)
    submission_reason = first_non_empty(
        submission_fields,
        "reason",
        "failure_reason",
        "decline_code",
        "failure_code",
        "code",
    )
    submission_code = first_non_empty(submission_fields, "code", "decline_code", "failure_code")
    submission_message = first_non_empty(submission_fields, "message", "failure_message", "error")
    return (
        f"keys=[{keys}], urls={len(urls)}, paypal_urls={paypal_count}, ba_approve_urls={ba_count}, "
        f"ignored_resource_urls={ignored_count}, submission_attempt={bool(submission)}, submission_state={submission_state or '未知'}, "
        f"submission_reason={submission_reason or '无'}, submission_code={submission_code or '无'}, "
        f"submission_message={submission_message or '无'}, "
        f"ctx_session={ctx.get('elements_session_id') or ''}"
    )


def first_non_empty(values: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = str(values.get(key) or "").strip()
        if value:
            return value
    return ""


def submission_attempt_failure_fields(submission: Any) -> dict[str, str]:
    wanted = {
        "error",
        "code",
        "message",
        "reason",
        "failure_reason",
        "decline_code",
        "failure_code",
        "failure_message",
    }
    found: dict[str, str] = {}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key or "").strip()
                if normalized in wanted and normalized not in found:
                    if isinstance(item, (str, int, float, bool)):
                        text = str(item).strip()
                    elif isinstance(item, dict):
                        text = str(
                            item.get("message")
                            or item.get("code")
                            or item.get("reason")
                            or item.get("type")
                            or ""
                        ).strip()
                    else:
                        text = ""
                    if text:
                        found[normalized] = text[:240]
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    if isinstance(submission, dict):
        walk(submission)
    return found


def submission_attempt_summary(submission: dict[str, Any]) -> str:
    if not submission:
        return "未找到 submission_attempt"
    fields = submission_attempt_failure_fields(submission)
    state = str(submission.get("state") or "未知").strip()
    reason = first_non_empty(fields, "reason", "failure_reason", "decline_code", "failure_code", "code")
    code = first_non_empty(fields, "code", "decline_code", "failure_code")
    message = first_non_empty(fields, "message", "failure_message", "error")
    parts = [f"state={state}"]
    if reason:
        parts.append(f"reason={reason}")
    if code:
        parts.append(f"code={code}")
    if message:
        parts.append(f"message={message}")
    return "，".join(parts)


def find_submission_attempt(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        item = payload.get("submission_attempt")
        if isinstance(item, dict):
            return item
        for value in payload.values():
            found = find_submission_attempt(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_submission_attempt(value)
            if found:
                return found
    return {}


class StripeRequiresApproval(Exception):
    pass


class ChatgptApproveBlocked(Exception):
    pass


APPROVE_BURST_RESULTS = {"blocked", "exception"}


def stripe_payment_page_redirect_url(
    stripe: Any,
    cs_id: str,
    stripe_pk: str,
    req: LongLinkRequest,
    ctx: dict[str, Any],
    timeout_seconds: float = 30,
    emit: Any | None = None,
) -> str:
    deadline = time.time() + max(1.0, float(timeout_seconds or 30))
    last_err = ""
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": ctx["elements_session_id"],
        "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
        "elements_session_client[locale]": str(ctx.get("locale") or locale_parts(req.payment_locale)[1]),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    if emit:
        emit("redirect", "confirm 未返回跳转，复用 Stripe session 上下文轮询 payment page。")
    poll_count = 0
    while time.time() < deadline:
        poll_count += 1
        if emit:
            emit("redirect", f"等待 PayPal BA 链...第 {poll_count} 次。")
        response = stripe.get(f"https://api.stripe.com/v1/payment_pages/{cs_id}", params=params, timeout=DEFAULT_TIMEOUT)
        if response.status_code == 200:
            payload = response.json() or {}
            redirect_url = extract_redirect_to_url(payload)
            if redirect_url:
                if emit:
                    emit("redirect", "轮询响应发现 PayPal 跳转候选。")
                return redirect_url
            submission = find_submission_attempt(payload)
            if submission.get("state") == "requires_approval":
                raise StripeRequiresApproval("payment page requires ChatGPT approval")
            if submission.get("state") == "failed":
                last_err = stripe_payload_diagnostics(payload, ctx)
                if emit:
                    emit("redirect", f"Stripe submission 已失败：{submission_attempt_summary(submission)}。")
                raise HTTPException(status_code=502, detail=f"stripe submission failed: {last_err}")
            last_err = stripe_payload_diagnostics(payload, ctx)
            if emit and (poll_count == 1 or poll_count % 5 == 0):
                if any(is_ignored_resource_url(item) for item in collect_urls(payload)):
                    emit("redirect", "检测到 Stripe 资源 URL，已忽略，不作为 PayPal 授权链。")
                emit("redirect", "仍在等待 PayPal BA approve 链。")
        else:
            last_err = f"http {response.status_code}: {response.text[:120]}"
            if emit and (poll_count == 1 or poll_count % 5 == 0):
                emit("redirect", f"Stripe 轮询暂未成功：HTTP {response.status_code}。")
        cancellable_sleep(1)
    raise HTTPException(status_code=504, detail=f"redirect url resolution timeout: {last_err}")


def chatgpt_approve(chatgpt: Any, cs_id: str, checkout: dict[str, Any], timeout_seconds: float = CHATGPT_TIMEOUT) -> None:
    country = checkout["billing_country"]
    processor_entity = processor_entity_for_country(country, checkout.get("processor_entity", ""))
    try:
        chatgpt.post(
            "https://chatgpt.com/backend-api/sentinel/ping",
            json={},
            headers={
                "Referer": "https://chatgpt.com/",
                "x-openai-target-path": "/backend-api/sentinel/ping",
                "x-openai-target-route": "/backend-api/sentinel/ping",
            },
            timeout=timeout_seconds,
        )
    except Exception:
        pass
    response = chatgpt.post(
        "https://chatgpt.com/backend-api/payments/checkout/approve",
        json={"checkout_session_id": cs_id, "processor_entity": processor_entity},
        headers={
            "Referer": f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}",
            "x-openai-target-path": "/backend-api/payments/checkout/approve",
            "x-openai-target-route": "/backend-api/payments/checkout/approve",
        },
        timeout=timeout_seconds,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=http_error_detail("chatgpt approve failed", response))
    try:
        result = (response.json() or {}).get("result")
    except Exception:
        result = ""
    normalized_result = str(result or "").strip().lower()
    if normalized_result in APPROVE_BURST_RESULTS:
        raise ChatgptApproveBlocked(f"chatgpt approve retryable result: {normalized_result!r}")
    if result != "approved":
        raise HTTPException(status_code=502, detail=f"chatgpt approve unexpected result: {result!r}")


def chatgpt_approve_blocked_burst(
    req: LongLinkRequest,
    cs_id: str,
    checkout: dict[str, Any],
    emit: Any | None = None,
) -> Any:
    attempts = APPROVE_BLOCKED_BURST_ATTEMPTS
    timeout_seconds = APPROVE_BLOCKED_BURST_TIMEOUT_SECONDS
    if emit:
        emit("approve", f"approve 返回 blocked/exception，启动并发复投：{attempts} 次 / {timeout_seconds:g} 秒。")
    acquired = 0
    for _ in range(attempts):
        ensure_not_cancelled()
        if not APPROVE_BURST_SEMAPHORE.acquire(timeout=timeout_seconds):
            break
        acquired += 1
    if acquired <= 0:
        raise HTTPException(status_code=429, detail="rate_limited: approve burst global limit reached")
    executor = ThreadPoolExecutor(max_workers=acquired)
    futures = []
    used_approve_bases: set[str] = set()
    for _ in range(acquired):
        ensure_not_cancelled()
        attempt_req = request_with_rotated_approve_session(req, used_bases=used_approve_bases)
        futures.append(executor.submit(lambda item=attempt_req: (chatgpt_approve(build_chatgpt_session(item), cs_id, checkout, timeout_seconds), item)[1]))
    deadline = time.time() + timeout_seconds
    last_error = "blocked/exception"
    try:
        for future in as_completed(futures, timeout=timeout_seconds):
            ensure_not_cancelled()
            try:
                approved_req = future.result()
                executor.shutdown(wait=False, cancel_futures=True)
                if emit:
                    emit("approve", "blocked 并发复投命中 approved。")
                return build_chatgpt_session(approved_req)
            except ChatgptApproveBlocked as exc:
                last_error = str(exc)
            except HTTPException as exc:
                last_error = str(exc.detail or exc)
            except Exception as exc:
                last_error = str(exc)
            if time.time() >= deadline:
                break
    except TimeoutError:
        pass
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        for _ in range(acquired):
            APPROVE_BURST_SEMAPHORE.release()
    raise HTTPException(
        status_code=502,
        detail=f"chatgpt approve blocked/exception burst failed: attempts={acquired}, timeout={timeout_seconds:g}s, last={short_error(last_error)}",
    )


def chatgpt_approve_with_retry(
    req: LongLinkRequest,
    cs_id: str,
    checkout: dict[str, Any],
    emit: Any | None = None,
) -> Any:
    last_error = ""
    verified_req: LongLinkRequest | None = None
    verified_country = ""
    verified_probe_label = ""
    max_attempts = checkout_retry_limit(req)
    used_approve_bases: set[str] = set()
    for attempt in range(1, max_attempts + 1):
        ensure_not_cancelled()
        country = approve_stage_country(req)
        if verified_req is None:
            if attempt > max_attempts:
                break
            attempt_req = request_with_rotated_approve_session(req, used_bases=used_approve_bases)
            probe_label = "统一代理" if single_proxy_mode(attempt_req) else f"approve {country}"
            expected_country = "" if single_proxy_mode(attempt_req) else country
            if emit:
                emit("approve", f"approve 出口检测 {attempt}/{max_attempts}：正在检测 {probe_label}。")
            probe = probe_proxy(probe_label, approve_stage_proxy(attempt_req), expected_country, required=True, parent_proxy=chain_parent_proxy(attempt_req))
            if not probe.ok:
                last_error = probe.error or f"{probe_label} 检测失败"
                if emit:
                    emit("approve", f"approve 第 {attempt} 次 {probe_label} 不可用：{last_error}")
                    if attempt < max_attempts:
                        emit("approve", f"approve 出口检测异常/国家不符，{PROXY_EXIT_RETRY_DELAY_SECONDS}秒后重试 {attempt + 1}/{max_attempts}。")
                if attempt < max_attempts:
                    cancellable_sleep(PROXY_EXIT_RETRY_DELAY_SECONDS)
                continue
            expected_ip = str(checkout.get("provider_proxy_ip") or checkout.get("checkout_proxy_ip") or "").strip()
            if bool(getattr(req, "strict_approve_same_proxy_ip", False)) and expected_ip and not proxy_ip_matches(expected_ip, probe.ip):
                last_error = f"proxy_ip_mismatch: expected={expected_ip}, approve={probe.ip or 'unknown'}"
                if emit:
                    emit(
                        "approve",
                        f"IP一致性检查失败：expected={expected_ip}，approve={probe.ip or 'unknown'}；更换同国家节点重试。",
                        proxy_stage="approve",
                        proxy_ip=probe.ip,
                        proxy_country_code=probe.country_code,
                        expected_proxy_ip=expected_ip,
                    )
                    if attempt < max_attempts:
                        emit("approve", f"approve IP不一致，{PROXY_EXIT_RETRY_DELAY_SECONDS}秒后重试 {attempt + 1}/{max_attempts}。")
                if attempt < max_attempts:
                    cancellable_sleep(PROXY_EXIT_RETRY_DELAY_SECONDS)
                continue
            verified_req = attempt_req
            verified_country = country
            verified_probe_label = probe_label
            if emit:
                emit(
                    "approve",
                    f"{probe_label} 出口：{probe.ip} / {probe.country_code} {probe.country}。",
                    proxy_stage="approve",
                    proxy_ip=probe.ip,
                    proxy_country_code=probe.country_code,
                    expected_country=expected_country,
                )
        attempt_req = verified_req
        country = verified_country or country
        try:
            if emit:
                emit("approve", f"approve 提交重试 {attempt}/{max_attempts}：正在建立 ChatGPT 会话。")
            chatgpt = build_chatgpt_session(attempt_req)
            if emit:
                emit("approve", f"approve 提交重试 {attempt}/{max_attempts}：正在提交 ChatGPT approve。")
            chatgpt_approve(chatgpt, cs_id, checkout)
            if emit:
                emit("approve", "ChatGPT approve 请求成功。")
            return chatgpt
        except ChatgptApproveBlocked:
            if req.enable_approve_blocked_burst:
                return chatgpt_approve_blocked_burst(req, cs_id, checkout, emit=emit)
            raise HTTPException(status_code=502, detail="chatgpt approve retryable result: blocked/exception")
        except HTTPException as exc:
            if not (is_retryable_provider_http_exception(exc) or is_retryable_chatgpt_http_exception(exc)):
                raise
            last_error = str(exc.detail or exc)
            if attempt < max_attempts:
                verified_req = None
            if emit:
                emit("approve", f"approve 返回可重试错误，准备更换 {country} session：{short_error(last_error)}")
                if attempt < max_attempts:
                    emit("approve", f"approve 可重试错误，{PROXY_EXIT_RETRY_DELAY_SECONDS}秒后重试提交 {attempt + 1}/{max_attempts}。")
            if attempt < max_attempts:
                cancellable_sleep(PROXY_EXIT_RETRY_DELAY_SECONDS)
        except Exception as exc:
            if not is_retryable_network_error(exc):
                raise
            last_error = str(exc)
            if attempt < max_attempts:
                verified_req = None
            if emit:
                emit("approve", f"approve 第 {attempt} 次网络超时/连接失败，正在更换 {country} session：{last_error}")
                if attempt < max_attempts:
                    emit("approve", f"approve 网络异常，{PROXY_EXIT_RETRY_DELAY_SECONDS}秒后重试提交 {attempt + 1}/{max_attempts}。")
            if attempt < max_attempts:
                cancellable_sleep(PROXY_EXIT_RETRY_DELAY_SECONDS)
    raise HTTPException(
        status_code=504,
        detail=f"ChatGPT approve 连续超时，出口检测/提交 {max_attempts} 次仍失败: {last_error}",
    )


def redirect_url_after_confirm(
    chatgpt: Any,
    stripe: Any,
    confirm_payload: dict[str, Any],
    cs_id: str,
    stripe_pk: str,
    ctx: dict[str, Any],
    checkout: dict[str, Any],
    req: LongLinkRequest,
    poll_stripe: Any | None = None,
    emit: Any | None = None,
) -> str:
    poll_session = poll_stripe or stripe
    if emit:
        emit("redirect", "Step 8：正在从 confirm payload 提取 Stripe/PayPal 跳转。")
    redirect_url = extract_redirect_to_url(confirm_payload)
    if redirect_url:
        return redirect_url
    submission = find_submission_attempt(confirm_payload)
    if submission.get("state") == "requires_approval":
        if emit:
            emit("approve", f"Step 7 chatgpt_approve：Stripe 要求 requires_approval，正在使用 {approve_stage_country(req)} 代理提交 ChatGPT approve。")
        chatgpt = chatgpt_approve_with_retry(req, cs_id, checkout, emit=emit)
        if emit:
            emit("approve", "Step 7 chatgpt_approve 已通过，继续轮询 PayPal BA approve 跳转。")
        return stripe_payment_page_redirect_url(poll_session, cs_id, stripe_pk, req, ctx, timeout_seconds=45, emit=emit)
    if submission.get("state") == "failed":
        diagnostics = stripe_payload_diagnostics(confirm_payload, ctx)
        if emit:
            emit("redirect", f"Stripe submission 已失败：{submission_attempt_summary(submission)}。")
        raise HTTPException(status_code=502, detail=f"stripe submission failed: {diagnostics}")
    try:
        return stripe_payment_page_redirect_url(poll_session, cs_id, stripe_pk, req, ctx, timeout_seconds=30, emit=emit)
    except StripeRequiresApproval:
        if emit:
            emit("approve", "Step 7 chatgpt_approve：轮询 payment page 发现 requires_approval，正在提交 ChatGPT approve。")
        chatgpt = chatgpt_approve_with_retry(req, cs_id, checkout, emit=emit)
        if emit:
            emit("approve", "Step 7 chatgpt_approve 已通过，继续轮询 PayPal BA approve 跳转。")
        return stripe_payment_page_redirect_url(poll_session, cs_id, stripe_pk, req, ctx, timeout_seconds=45, emit=emit)


def resolve_external_redirect(stripe: Any, redirect_url: str, preferred_hosts: tuple[str, ...] = (), max_hops: int = 5) -> str:
    current = str(redirect_url or "").strip()
    seen: set[str] = set()
    for _ in range(max(1, int(max_hops or 1))):
        if not current:
            return ""
        if is_paypal_ba_approve_url(current):
            return current
        if current in seen:
            return current
        seen.add(current)
        try:
            response = stripe.get(current, allow_redirects=False, timeout=DEFAULT_TIMEOUT)
        except Exception:
            return current
        if response.status_code not in (301, 302, 303, 307, 308):
            next_url = extract_browser_redirect_url(current, getattr(response, "text", "") or "")
            if next_url and next_url != current:
                current = next_url
                continue
            return current
        location = str(response.headers.get("Location") or "").strip()
        if not location:
            return current
        current = urljoin(current, location)
    return current


def create_provider_link(
    chatgpt: Any,
    checkout: dict[str, Any],
    init_payload: dict[str, Any],
    stripe_hosted_url: str,
    req: LongLinkRequest,
    provider_proxy: str = "",
    promotion_proxy: str = "",
    billing: dict[str, str] | None = None,
    emit: Any | None = None,
) -> dict[str, str]:
    stripe_pk = stripe_key_for_request(req, checkout)
    if emit:
        emit("provider", "正在创建 Stripe/Provider 会话。")
    stripe = build_stripe_session(req, proxy_override=provider_proxy)
    poll_stripe = build_stripe_session(req, proxy_override=promotion_proxy or promotion_stage_proxy(req))
    ctx = stripe_context(checkout["cs_id"], init_payload, req)
    stripe_amount = str(ctx.get("checkout_amount") or expected_amount(init_payload))
    stripe_amount_source = stripe_amount_info(init_payload)[1]
    if emit:
        emit(
            "stripe_amount",
            f"Stripe 金额：amount={stripe_amount} currency={ctx.get('currency') or ''} source={stripe_amount_source}。",
            stripe_amount=stripe_amount,
            stripe_amount_source=stripe_amount_source,
            currency=ctx.get("currency") or "",
        )
    if req.require_zero_stripe_amount:
        if is_unknown_amount(stripe_amount):
            raise HTTPException(
                status_code=402,
                detail=(
                    "stripe_amount_unknown: "
                    f"amount=unknown, currency={ctx.get('currency') or ''}, source={stripe_amount_source}"
                ),
            )
        if not is_zero_amount(stripe_amount):
            raise HTTPException(
                status_code=402,
                detail=(
                    "stripe_amount_nonzero: "
                    f"amount={stripe_amount}, currency={ctx.get('currency') or ''}, source={stripe_amount_source}"
                ),
            )
    pm_country = effective_payment_method_country(req)
    billing = dict(billing or billing_for_country(pm_country))
    if emit:
        emit("billing", f"已选择 PM 账单 {billing['country']} 随机资料：{billing['name']} / {billing['city']}。")
        emit("payment_method", "Step 5 stripe_create_payment_method：正在向 Stripe 创建 PayPal payment_method。")
    pm_id = stripe_create_payment_method(stripe, checkout["cs_id"], stripe_pk, billing, ctx)
    if emit:
        emit("payment_method", f"Step 5 stripe_create_payment_method 成功：{pm_id}。")
        emit("confirm", "Step 6 stripe_confirm：正在绑定 pm_id 并确认 Stripe checkout。")
    confirm_payload = stripe_confirm(
        stripe,
        checkout["cs_id"],
        pm_id,
        stripe_pk,
        init_payload,
        ctx,
        checkout,
        req,
        stripe_hosted_url,
    )
    if emit:
        emit("confirm", "Step 6 stripe_confirm 已返回，正在解析 PayPal/approval 跳转。")
    stripe_redirect_url = redirect_url_after_confirm(
        chatgpt,
        stripe,
        confirm_payload,
        checkout["cs_id"],
        stripe_pk,
        ctx,
        checkout,
        req,
        poll_stripe=poll_stripe,
        emit=emit,
    )
    if emit:
        emit("stripe_redirect", f"Step 8 捕获 Stripe 中转链：{stripe_redirect_url}")
        emit("redirect", "Step 9 resolve_external_redirect：正在跟随 Stripe → pay.openai.com → PayPal 跳转并提取 BA 链。")
    provider_url = (
        stripe_redirect_url
        if is_paypal_ba_approve_url(stripe_redirect_url)
        else resolve_external_redirect(poll_stripe, stripe_redirect_url, preferred_hosts=("paypal.com",))
    )
    if not is_paypal_ba_approve_url(provider_url):
        resource_hint = "仅发现 Stripe 资源 URL，未发现 PayPal BA approve 链；" if is_ignored_resource_url(provider_url) else ""
        pm_redirect_hint = (
            "stripe_pm_redirect_not_ba: pm-redirects.stripe.com 只是 Stripe 中间跳转，不是真正 0 元 PayPal BA approve 链；"
            if is_stripe_pm_redirect_url(stripe_redirect_url) or is_stripe_pm_redirect_url(provider_url)
            else ""
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"{pm_redirect_hint}{resource_hint}未提取到最终 PayPal BA approve 链；成功标准必须为 "
                "https://www.paypal.com/agreements/approve?ba_token=...；"
                f"当前结果: {provider_url or stripe_redirect_url}"
            ),
        )
    if emit:
        emit("done", "Step 9 完成：已提取到 PayPal BA approve 链。")
    return {
        "payment_method_id": pm_id,
        "stripe_redirect_url": stripe_redirect_url,
        "provider_redirect_url": provider_url,
        "long_url": provider_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
    }


def create_provider_link_with_retry(
    chatgpt: Any,
    checkout: dict[str, Any],
    req: LongLinkRequest,
    emit: Any | None = None,
    max_attempts_override: int | None = None,
    attempt_offset: int = 0,
    attempt_limit: int | None = None,
) -> dict[str, str]:
    last_error = ""
    country = provider_stage_country(req)
    promotion_country = promotion_stage_country(req)
    label = provider_stage_label(req)
    configured_attempts = int(getattr(req, "provider_retry_limit", PROXY_EXIT_RETRY_ATTEMPTS) or PROXY_EXIT_RETRY_ATTEMPTS)
    max_attempts = max(1, min(20, int(max_attempts_override or configured_attempts)))
    display_limit = max(1, min(20, int(attempt_limit or (attempt_offset + max_attempts))))
    used_provider_bases: set[str] = set()
    used_promotion_bases: set[str] = set()
    for attempt in range(1, max_attempts + 1):
        ensure_not_cancelled()
        display_attempt = attempt_offset + attempt
        if provider_uses_checkout(req):
            rotated_proxy = provider_stage_proxy(req)
        elif single_proxy_mode(req):
            rotated_proxy = provider_stage_proxy(req)
        else:
            rotated_proxy = rotated_stage_proxy_for_attempt(req, "provider", country, provider_stage_proxy(req), used_bases=used_provider_bases)
        attempt_req = request_with_stage_proxy(req, "provider", rotated_proxy, country)
        rotated_promotion_proxy = rotated_stage_proxy_for_attempt(
            attempt_req,
            "promotion",
            promotion_country,
            promotion_stage_proxy(attempt_req),
            used_bases=used_promotion_bases,
        )
        attempt_req = request_with_stage_proxy(
            attempt_req,
            "promotion",
            rotated_promotion_proxy,
            promotion_country,
        )
        provider_proxy = provider_stage_proxy(attempt_req)
        promotion_proxy = promotion_stage_proxy(attempt_req)
        if emit:
            emit(
                "link_attempt",
                f"提链尝试 {display_attempt}/{display_limit}：左池={country}，右池={promotion_country}。",
                link_attempt_count=display_attempt,
                link_attempt_limit=display_limit,
                provider_country=country,
                promotion_country=promotion_country,
        )
        if emit:
            if provider_uses_checkout(req):
                session_text = "复用 checkout sticky session"
            else:
                session_text = "复用统一代理" if single_proxy_mode(req) else f"轮换 {country} session"
            emit("provider", f"provider 第 {display_attempt}/{display_limit} 次：{session_text}。")
            emit("proxy", f"provider 第 {display_attempt}/{display_limit} 次：正在检测 {label}。")
        provider_probe = check_provider_proxy(attempt_req)
        if not provider_probe.ok:
            last_error = provider_probe.error or f"{label} 代理不可用"
            if emit:
                emit("proxy", f"{label} session 不可用，准备更换：{last_error}")
                if attempt < max_attempts:
                    emit("proxy", f"provider 出口检测异常/国家不符，{PROXY_EXIT_RETRY_DELAY_SECONDS}秒后重试 {attempt + 1}/{max_attempts}。")
            if attempt < max_attempts:
                cancellable_sleep(PROXY_EXIT_RETRY_DELAY_SECONDS)
            continue
        expected_ip = str(checkout.get("checkout_proxy_ip") or "").strip()
        if bool(getattr(req, "strict_same_proxy_ip", True)) and expected_ip and not proxy_ip_matches(expected_ip, provider_probe.ip):
            last_error = f"proxy_ip_mismatch: checkout={expected_ip}, provider={provider_probe.ip or 'unknown'}"
            if emit:
                emit(
                    "proxy",
                    f"IP一致性检查失败：checkout={expected_ip}，provider={provider_probe.ip or 'unknown'}；更换同国家节点重试。",
                    proxy_stage="provider",
                    proxy_ip=provider_probe.ip,
                    proxy_country_code=provider_probe.country_code,
                    expected_proxy_ip=expected_ip,
                )
            if attempt < max_attempts:
                cancellable_sleep(PROXY_EXIT_RETRY_DELAY_SECONDS)
            continue
        checkout["provider_proxy_ip"] = provider_probe.ip
        checkout["provider_proxy_country_code"] = provider_probe.country_code
        if emit:
            emit(
                "proxy",
                f"{label} 出口：{provider_probe.ip} / {provider_probe.country_code} {provider_probe.country}。",
                proxy_stage="provider",
                proxy_ip=provider_probe.ip,
                proxy_country_code=provider_probe.country_code,
                expected_country=country,
            )
            emit("proxy", f"右池 {promotion_country}：正在检测 promotion/update/poll 出口。")
        promotion_probe = probe_proxy(
            f"promotion/update/poll {promotion_country}",
            promotion_proxy,
            promotion_country,
            required=True,
            parent_proxy=chain_parent_proxy(attempt_req),
        )
        if not promotion_probe.ok:
            last_error = promotion_probe.error or f"promotion/update/poll {promotion_country} 代理不可用"
            if emit:
                emit("proxy", f"右池 session 不可用，准备更换：{last_error}")
            if attempt < max_attempts:
                cancellable_sleep(PROXY_EXIT_RETRY_DELAY_SECONDS)
            continue
        checkout["promotion_proxy_ip"] = promotion_probe.ip
        checkout["promotion_proxy_country_code"] = promotion_probe.country_code
        if emit:
            emit(
                "proxy",
                f"右池 {promotion_country} 出口：{promotion_probe.ip} / {promotion_probe.country_code} {promotion_probe.country}。",
                proxy_stage="promotion",
                proxy_ip=promotion_probe.ip,
                proxy_country_code=promotion_probe.country_code,
                expected_country=promotion_country,
            )
        try:
            init_payload, stripe_hosted_url, billing = prepare_paypal_after_sync(
                chatgpt,
                checkout,
                attempt_req,
                provider_proxy=provider_proxy,
                promotion_proxy=promotion_proxy,
                emit=emit,
            )
            if emit:
                emit("stripe_init", "Step 4 stripe_init 成功，已确认 Stripe 前端支付环境。")
                emit(
                    "stripe_methods",
                    f"Stripe init 支付方式线索：{summarize_payment_method_hints(init_payload)}",
                    paypal_hint=init_payload_has_paypal_hint(init_payload),
                )
            provider = create_provider_link(
                chatgpt,
                checkout,
                init_payload,
                stripe_hosted_url,
                attempt_req,
                provider_proxy=provider_proxy,
                promotion_proxy=promotion_proxy,
                billing=billing,
                emit=emit,
            )
            provider["stripe_hosted_url"] = stripe_hosted_url
            provider.setdefault("stripe_amount", expected_amount(init_payload))
            provider.setdefault("stripe_amount_source", stripe_amount_info(init_payload)[1])
            provider["link_attempt_count"] = str(display_attempt)
            provider["link_attempt_limit"] = str(display_limit)
            if emit:
                emit(
                    "link_attempt",
                    f"提链尝试 {display_attempt}/{display_limit} 成功。",
                    link_attempt_count=display_attempt,
                    link_attempt_limit=display_limit,
                    provider_country=country,
                )
            return provider
        except HTTPException as exc:
            if not is_retryable_provider_flow_exception(exc):
                raise
            last_error = str(exc.detail or exc)
            if emit:
                emit(
                    "link_attempt",
                    f"提链尝试 {display_attempt}/{display_limit} 失败：{short_error(last_error)}",
                    link_attempt_count=display_attempt,
                    link_attempt_limit=display_limit,
                    provider_country=country,
                )
            if emit:
                emit("provider", f"左右双池流程返回可重试错误，准备更换 session：{short_error(last_error)}")
                if attempt < max_attempts:
                    emit("provider", f"provider/Stripe 可重试错误，{PROXY_EXIT_RETRY_DELAY_SECONDS}秒后重试 {attempt + 1}/{max_attempts}。")
            if attempt < max_attempts:
                cancellable_sleep(PROXY_EXIT_RETRY_DELAY_SECONDS)
        except Exception as exc:
            if not is_retryable_network_error(exc):
                raise
            last_error = str(exc)
            if emit:
                emit(
                    "link_attempt",
                    f"提链尝试 {display_attempt}/{display_limit} 网络异常：{short_error(last_error)}",
                    link_attempt_count=display_attempt,
                    link_attempt_limit=display_limit,
                    provider_country=country,
                )
                emit("provider", f"左右双池网络异常，准备更换 session：{short_error(last_error)}")
                if attempt < max_attempts:
                    emit("provider", f"provider 网络异常，{PROXY_EXIT_RETRY_DELAY_SECONDS}秒后重试 {attempt + 1}/{max_attempts}。")
            if attempt < max_attempts:
                cancellable_sleep(PROXY_EXIT_RETRY_DELAY_SECONDS)
    raise HTTPException(
        status_code=504,
        detail=f"provider 提链已达到上限 {max_attempts} 次仍失败: {short_error(last_error)}",
    )


app = FastAPI(title="OpenAI Pay Long Link")
app.mount("/public", StaticFiles(directory=PUBLIC_DIR), name="public")


@app.get("/")
def index() -> FileResponse:
    bump_stats("total_visits")
    return FileResponse(PUBLIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/local-run-key")
def local_run_key(request: Request) -> dict[str, Any]:
    require_loopback_client(request)
    return local_run_key_response()


@app.get("/api/stats")
def stats(request: Request) -> dict[str, Any]:
    require_local_run_key(request)
    return get_stats_snapshot()


@app.post("/api/batch-results", response_model=BatchResultSaveResponse)
def save_batch_results(req: BatchResultSaveRequest, request: Request) -> BatchResultSaveResponse:
    require_local_run_key(request)
    return save_batch_result_file(req)


@app.post("/api/check-proxy", response_model=ProxyCheckResponse)
def check_proxy(req: ProxyCheckRequest, request: Request) -> ProxyCheckResponse:
    require_local_run_key(request)
    results = check_request_proxies(req)  # type: ignore[arg-type]
    mark_same_exit(results)
    return ProxyCheckResponse(ok=all(item.ok for item in results), results=results)


@app.post("/api/long-link", response_model=LongLinkResponse)
def generate_long_link(req: LongLinkRequest, request: Request) -> LongLinkResponse:
    require_local_run_key(request)
    result = generate_long_link_payload(req)
    bump_stats("total_success")
    return result


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


def cleanup_run_jobs(now: float | None = None) -> None:
    current_time = time.time() if now is None else now
    with RUN_JOBS_LOCK:
        expired = [
            run_id
            for run_id, job in RUN_JOBS.items()
            if bool(job.get("done")) and current_time - float(job.get("completed_at") or job.get("created_at") or current_time) > RUN_JOB_TTL_SECONDS
        ]
        for run_id in expired:
            RUN_JOBS.pop(run_id, None)
        if len(RUN_JOBS) > RUN_JOB_MAX_ITEMS:
            ordered = sorted(RUN_JOBS.items(), key=lambda item: float(item[1].get("created_at") or 0))
            for run_id, job in ordered[: max(0, len(RUN_JOBS) - RUN_JOB_MAX_ITEMS)]:
                if bool(job.get("done")):
                    RUN_JOBS.pop(run_id, None)


def active_run_job_count() -> int:
    with RUN_JOBS_LOCK:
        return sum(1 for job in RUN_JOBS.values() if not bool(job.get("done")))


def ensure_run_capacity() -> None:
    if active_run_job_count() >= MAX_ACTIVE_RUN_JOBS:
        raise HTTPException(status_code=429, detail="rate_limited: active run job limit reached")


def current_cancel_event() -> threading.Event | None:
    event = getattr(CANCEL_CONTEXT, "event", None)
    return event if isinstance(event, threading.Event) else None


def is_cancel_requested() -> bool:
    event = current_cancel_event()
    return bool(event and event.is_set())


def ensure_not_cancelled() -> None:
    if is_cancel_requested():
        raise HTTPException(status_code=499, detail="cancelled: run cancelled by user")


def cancellable_sleep(seconds: float) -> None:
    event = current_cancel_event()
    if event is None:
        time.sleep(seconds)
        return
    if event.wait(max(0.0, seconds)):
        raise HTTPException(status_code=499, detail="cancelled: run cancelled by user")


def default_stats() -> dict[str, Any]:
    return {
        "total_visits": 0,
        "total_success": 0,
        "updated_at": 0.0,
    }


def load_stats() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATS_FILE.exists():
        return default_stats()
    payload = read_json(STATS_FILE, {}, expected_schema_version=1)
    stats = default_stats()
    if isinstance(payload, dict):
        stats["total_visits"] = int(payload.get("total_visits") or 0)
        stats["total_success"] = int(payload.get("total_success") or 0)
        stats["updated_at"] = float(payload.get("updated_at") or 0.0)
    return stats


def save_stats(stats: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_json(STATS_FILE, stats, schema_version=1)


def bump_stats(field: str) -> dict[str, Any]:
    with STATS_LOCK:
        stats = load_stats()
        stats[field] = int(stats.get(field) or 0) + 1
        stats["updated_at"] = time.time()
        save_stats(stats)
        return stats


def get_stats_snapshot() -> dict[str, Any]:
    with STATS_LOCK:
        return load_stats()


def safe_result_filename(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    name = name.strip("._-") or "latest_matrix_result"
    if not name.lower().endswith(".json"):
        name += ".json"
    return name[:120]


def local_run_key_response() -> dict[str, Any]:
    return {
        "ok": True,
        "run_key": LOCAL_RUN_KEY,
        "expected_matrix_total": EXPECTED_MATRIX_TOTAL,
    }


def require_loopback_client(request: Request) -> None:
    host = str(request.client.host if request.client else "").strip()
    if host == "testclient":
        return
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError as error:
        raise HTTPException(status_code=403, detail="local run key is available only from loopback") from error
    if not address.is_loopback:
        raise HTTPException(status_code=403, detail="local run key is available only from loopback")


def require_local_run_key(request: Request) -> None:
    supplied = str(request.headers.get("X-Local-Run-Key") or "")
    if not supplied or not secrets.compare_digest(supplied, LOCAL_RUN_KEY):
        raise HTTPException(status_code=403, detail="invalid local run key")


def short_hash(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:12]


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if lowered in {"long_url", "stripe_hosted_url", "provider_redirect_url", "stripe_redirect_url"}:
                text = str(item or "")
                result[f"{key_text}_present"] = bool(text)
                result[f"{key_text}_hash"] = short_hash(text)
                continue
            if "token" in lowered or "authorization" in lowered:
                result[key_text] = "[redacted]"
                continue
            result[key_text] = redact_sensitive(item)
        return result
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if not isinstance(value, str):
        return value
    text = value
    text = re.sub(r"(?i)(accessToken|access_token|authorization|bearer)\s*[:=]\s*[^,\s\"']+", r"\1=[redacted]", text)
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?i)\b(?:https?|socks4|socks5|socks5h)://([^:/@\s]+):([^@\s]+)@",
        lambda match: match.group(0).replace(match.group(1), "user").replace(match.group(2), "pass"),
        text,
    )
    text = re.sub(r"\b([^:@\s]+):([^@\s]+)@([A-Za-z0-9.-]+:\d+)", r"user:pass@\3", text)
    text = re.sub(r"(https?://(?:pay|checkout|api)?[^ \t\r\n\"']+)", lambda match: f"[url:{short_hash(match.group(1))}]", text)
    return text


def save_batch_result_file(req: BatchResultSaveRequest) -> BatchResultSaveResponse:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    filename = safe_result_filename(req.filename)
    path = DATA_DIR / filename
    payload = redact_sensitive(dict(req.payload or {}))
    payload.setdefault("saved_at", time.time())
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(encoded.encode("utf-8")) > MAX_BATCH_RESULT_BYTES:
        raise HTTPException(status_code=413, detail="batch result payload too large")
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    if (
        int(summary.get("total") or 0) != EXPECTED_MATRIX_TOTAL
        or int(summary.get("billing_country_count") or 0) != EXPECTED_BILLING_COUNTRY_COUNT
    ):
        raise HTTPException(
            status_code=400,
            detail=f"result version mismatch: expected {EXPECTED_BILLING_COUNTRY_COUNT} countries / {EXPECTED_MATRIX_TOTAL} tasks",
        )
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(encoded, encoding="utf-8")
    tmp_path.replace(path)
    return BatchResultSaveResponse(ok=True, filename=filename, path=str(path))


def make_log_event(run_id: str, step: str, message: str, started_at: float, **extra: Any) -> dict[str, Any]:
    now = time.time()
    return {
        "type": "log",
        "run_id": run_id,
        "step": step,
        "message": redact_sensitive(compact_log_message(step, message)),
        "ts": now,
        "elapsed_ms": int((now - started_at) * 1000),
        **redact_sensitive(extra),
    }


def make_error_event(run_id: str, message: str, started_at: float, status_code: int | None = None) -> dict[str, Any]:
    now = time.time()
    event: dict[str, Any] = {
        "type": "error",
        "run_id": run_id,
        "step": "error",
        "message": redact_sensitive(compact_log_message("error", message)),
        "ts": now,
        "elapsed_ms": int((now - started_at) * 1000),
    }
    if status_code is not None:
        event["status_code"] = status_code
    return event


def compact_log_message(step: str, message: str) -> str:
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    if not text:
        return ""
    if step == "summary" or text.startswith("组合结果："):
        return short_error(text, 360)
    if "运行编号：" in text:
        return text
    if "尝试组合" in text:
        match = re.search(r"尝试组合\s+(\d+/\d+).*checkout=([A-Z]{2})/([A-Z]{3})，PM=([A-Z]{2})", text)
        return f"组合 {match.group(1)}：{match.group(2)}+{match.group(4)} / {match.group(3)}" if match else short_error(text, 80)
    if "开始执行 PP 链提取流程" in text:
        return "开始执行，组合回退已开启"
    if "当前组合：" in text:
        match = re.search(r"checkout账单=([A-Z]{2})/([A-Z]{3})，PM账单=([A-Z]{2})，provider代理=([A-Z]{2})", text)
        return f"账单 {match.group(1)}/{match.group(2)}，PM {match.group(3)}，provider {match.group(4)}" if match else short_error(text, 80)
    if "检测 provider" in text and "出口" in text:
        match = re.search(r"检测 provider\s+([A-Z]{2})", text)
        return f"预检 {match.group(1)}，分阶段检测 checkout/approve" if match else "预检 provider 出口"
    if "provider" in text and "自动轮换" in text:
        match = re.search(r"provider\s+([A-Z]{2})\s+第\s+(\d+/\d+)", text)
        return f"轮换 {match.group(1)} 节点 {match.group(2)}" if match else "轮换 provider 节点"
    if "provider 第" in text and "轮换" in text and "session" in text:
        match = re.search(r"provider 第\s+(\d+/\d+).*轮换\s+([A-Z]{2})", text)
        return f"provider 重试 {match.group(1)}，换 {match.group(2)} 节点" if match else "provider 换节点"
    if "自动切换下一个组合" in text:
        match = re.search(r"checkout=([A-Z]{2})，PM=([A-Z]{2})", text)
        return f"切换组合：{match.group(1)}+{match.group(2)}" if match else "切换下一个组合"
    if "未拿到 BA approve" in text:
        match = re.search(r"组合\s+([A-Z]{2}\+[A-Z]{2})", text)
        return f"组合 {match.group(1)} 失败，未拿到 BA 链" if match else "当前组合失败，未拿到 BA 链"
    if "失败且不可回退" in text:
        match = re.search(r"组合\s+([A-Z]{2}\+[A-Z]{2})", text)
        return f"组合 {match.group(1)} 失败，停止运行" if match else "组合失败，停止运行"
    if "Stripe submission 已失败" in text:
        reason = re.search(r"reason=([^，。]+)", text)
        return f"Stripe 拒绝：{reason.group(1)}" if reason else "Stripe 拒绝付款"
    if "Stripe key 来源" in text:
        match = re.search(r"Stripe key 来源：([^；。]+)", text)
        return f"Stripe key：{match.group(1)}" if match else "Stripe key 已确认"
    if "已选择 PM 账单" in text:
        match = re.search(r"PM 账单\s+([A-Z]{2}).*?：([^/。]+)", text)
        return f"PM 资料：{match.group(1)} / {match.group(2).strip()}" if match else "PM 资料已生成"
    if "正在从 confirm payload 提取 PayPal 跳转" in text:
        return "解析 confirm 跳转"
    if "Stripe 要求 ChatGPT approve" in text:
        return "需要 approve，切换 approve 阶段代理"
    if "正在建立 ChatGPT 会话" in text:
        match = re.search(r"approve 第\s+(\d+/\d+)", text)
        return f"建立 ChatGPT 会话 {match.group(1)}" if match else "建立 ChatGPT 会话"
    if "正在提交 ChatGPT approve" in text:
        match = re.search(r"approve 第\s+(\d+/\d+)", text)
        return f"提交 approve {match.group(1)}" if match else "提交 approve"
    if "继续轮询 PayPal BA approve 跳转" in text:
        return "继续轮询 BA 链"
    if "provider 代理检测通过" in text:
        match = re.search(r"provider\s+([A-Z]{2}):\s*([^/。]+)\s*/\s*([A-Z]{2})", text)
        return f"{match.group(1)} 预检通过：{match.group(2).strip()} / {match.group(3)}" if match else "provider 预检通过"
    if "provider" in text and "出口" in text:
        match = re.search(r"provider\s+([A-Z]{2})\s+出口：([^/。]+)\s*/\s*([A-Z]{2})", text)
        return f"{match.group(1)} 出口：{match.group(2).strip()} / {match.group(3)}" if match else "provider 出口通过"
    if re.search(r"checkout [A-Z]{2} 出口", text):
        match = re.search(r"checkout ([A-Z]{2}) 出口：([^/。]+)\s*/\s*([A-Z]{2})", text)
        return f"checkout {match.group(1)}：{match.group(2).strip()} / {match.group(3)}" if match else "checkout 出口通过"
    if re.search(r"approve [A-Z]{2} 出口", text):
        match = re.search(r"approve ([A-Z]{2}) 出口：([^/。]+)\s*/\s*([A-Z]{2})", text)
        return f"approve {match.group(1)}：{match.group(2).strip()} / {match.group(3)}" if match else "approve 出口通过"
    if "checkout 创建成功" in text:
        match = re.search(r"/\s*([A-Z]{2})\s*/\s*([A-Z]{3})", text)
        return f"checkout 成功：{match.group(1)}/{match.group(2)}" if match else "checkout 成功"
    if "正在使用 JP 代理创建 ChatGPT checkout" in text or "正在使用 统一代理创建 ChatGPT checkout" in text:
        return "准备 checkout"
    if "正在切换到 provider 阶段代理" in text:
        match = re.search(r"provider 阶段代理：([A-Z]{2})", text)
        return f"切换 provider {match.group(1)}" if match else "切换 provider"
    if "正在创建 Stripe/Provider 会话" in text:
        return "创建 provider 会话"
    if "PayPal payment_method 创建成功" in text:
        return "PM 创建成功"
    if "Stripe confirm 已返回" in text:
        return "confirm 成功，解析跳转"
    if "ChatGPT approve 请求成功" in text or "ChatGPT approve 已通过" in text:
        return "approve 成功"
    if re.search(r"[A-Z]{2} session 不可用", text):
        match = re.search(r"([A-Z]{2}) session 不可用", text)
        return f"{match.group(1)} 节点不可用，换节点" if match else "节点不可用，换节点"
    if re.search(r"provider [A-Z]{2} (session )?不可用", text):
        match = re.search(r"provider ([A-Z]{2})", text)
        return f"{match.group(1)} 节点不可用，换节点" if match else "provider 节点不可用，换节点"
    if "provider 网络异常" in text:
        return "provider 网络异常，换同国家节点"
    if "provider US 代理检测未通过" in text:
        return "provider US 代理检测失败"
    if "代理检测未通过" in text:
        return "代理检测失败"
    if "网络超时/连接失败" in text:
        return "网络异常，换节点"
    if "正在检测 JP 出口" in text:
        return "检测 JP 出口"
    if "正在检测 US 出口" in text:
        return "检测 US 出口"
    if "正在检测 AU 出口" in text:
        return "检测 AU 出口"
    if "正在创建 ChatGPT checkout" in text:
        return "创建 checkout"
    if "正在请求 Stripe init" in text:
        return "请求 Stripe init"
    if "Stripe init 成功" in text:
        return "Stripe init 成功"
    if "正在请求 Stripe 创建 PayPal payment_method" in text:
        return "创建 PM"
    if "正在请求 Stripe confirm" in text:
        return "请求 confirm"
    if "等待 PayPal BA 链" in text:
        return text.replace("PayPal BA 链", "BA 链")
    if len(text) > 140:
        return short_error(text, 140)
    return text


@app.post("/api/long-link/stream")
def generate_long_link_stream(req: LongLinkRequest, request: Request) -> StreamingResponse:
    require_local_run_key(request)
    events: queue.Queue[dict[str, Any] | None] = queue.Queue()
    run_id = new_run_id()
    started_at = time.time()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{run_id}.ndjson"
    heartbeat_seconds = 1.5

    def write_event(event: dict[str, Any]) -> None:
        event = redact_sensitive(event)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def emit(step: str, message: str, **extra: Any) -> None:
        event = make_log_event(run_id, step, message, started_at, **extra)
        event = redact_sensitive(event)
        write_event(event)
        events.put(event)

    def encode_event(event: dict[str, Any]) -> str:
        return json.dumps(event, ensure_ascii=False) + "\n" + (" " * 16384) + "\n"

    def worker() -> None:
        try:
            emit("run", f"运行编号：{run_id}", log_path=str(log_path))
            result = generate_long_link_payload(req, emit=emit)
            bump_stats("total_success")
            now = time.time()
            event = redact_sensitive({"type": "result", "run_id": run_id, "data": result.model_dump(), "ts": now, "elapsed_ms": int((now - started_at) * 1000)})
            write_event(event)
            events.put(event)
        except HTTPException as exc:
            event = make_error_event(run_id, str(exc.detail), started_at, exc.status_code)
            event = redact_sensitive(event)
            write_event(event)
            events.put(event)
        except Exception as exc:
            event = make_error_event(run_id, str(exc), started_at)
            event = redact_sensitive(event)
            write_event(event)
            events.put(event)
        finally:
            events.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream() -> Any:
        heartbeat_count = 0
        while True:
            try:
                event = events.get(timeout=heartbeat_seconds)
            except queue.Empty:
                heartbeat_count += 1
                yield encode_event(
                    {
                        "type": "heartbeat",
                        "run_id": run_id,
                        "step": "heartbeat",
                        "message": f"仍在执行当前步骤...{heartbeat_count}",
                        "ts": time.time(),
                        "elapsed_ms": int((time.time() - started_at) * 1000),
                    }
                )
                continue
            if event is None:
                break
            yield encode_event(event)

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def create_long_link_job(req: LongLinkRequest) -> dict[str, str]:
    cleanup_run_jobs()
    run_id = new_run_id()
    started_at = time.time()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{run_id}.ndjson"
    cancel_event = threading.Event()
    job: dict[str, Any] = {
        "run_id": run_id,
        "log_path": str(log_path),
        "events": [],
        "event_offset": 0,
        "done": False,
        "cancel_event": cancel_event,
        "cancelled": False,
        "created_at": started_at,
        "started_at": started_at,
        "last_heartbeat": 0.0,
    }
    with RUN_JOBS_LOCK:
        active_count = sum(1 for item in RUN_JOBS.values() if not bool(item.get("done")))
        if active_count >= MAX_ACTIVE_RUN_JOBS:
            raise HTTPException(status_code=429, detail="rate_limited: active run job limit reached")
        RUN_JOBS[run_id] = job

    def append_event(event: dict[str, Any]) -> None:
        event = redact_sensitive(event)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        with RUN_JOBS_LOCK:
            current = RUN_JOBS.get(run_id)
            if current is not None:
                current["events"].append(event)
                if len(current["events"]) > RUN_JOB_MAX_EVENTS:
                    overflow = len(current["events"]) - RUN_JOB_MAX_EVENTS
                    del current["events"][:overflow]
                    current["event_offset"] = int(current.get("event_offset") or 0) + overflow

    def emit(step: str, message: str, **extra: Any) -> None:
        append_event(make_log_event(run_id, step, message, started_at, **extra))

    def worker() -> None:
        CANCEL_CONTEXT.event = cancel_event
        try:
            emit("run", f"运行编号：{run_id}", log_path=str(log_path))
            result = generate_long_link_payload(req, emit=emit)
            now = time.time()
            bump_stats("total_success")
            append_event({"type": "result", "run_id": run_id, "data": redact_sensitive(result.model_dump()), "ts": now, "elapsed_ms": int((now - started_at) * 1000)})
        except HTTPException as exc:
            append_event(make_error_event(run_id, str(exc.detail), started_at, exc.status_code))
        except Exception as exc:
            append_event(make_error_event(run_id, str(exc), started_at))
        finally:
            CANCEL_CONTEXT.event = None
            with RUN_JOBS_LOCK:
                current = RUN_JOBS.get(run_id)
                if current is not None:
                    current["done"] = True
                    current["completed_at"] = time.time()

    threading.Thread(target=worker, daemon=True).start()
    return {"run_id": run_id, "log_path": str(log_path)}


@app.post("/api/long-link/start")
def start_long_link_job(req: LongLinkRequest, request: Request) -> dict[str, str]:
    require_local_run_key(request)
    return create_long_link_job(req)


@app.post("/api/long-link/cancel/{run_id}")
def cancel_long_link_job(run_id: str, request: Request) -> dict[str, Any]:
    require_local_run_key(request)
    cancel_event_payload: dict[str, Any] | None = None
    log_path = ""
    with RUN_JOBS_LOCK:
        job = RUN_JOBS.get(run_id)
        if job is None:
            raise HTTPException(status_code=404, detail="运行编号不存在或已过期")
        event = job.get("cancel_event")
        if isinstance(event, threading.Event):
            event.set()
        job["cancelled"] = True
        started_at = float(job.get("started_at") or job.get("created_at") or time.time())
        log_path = str(job.get("log_path") or "")
        cancel_event_payload = make_log_event(run_id, "cancelled", "cancelled: 已请求取消当前任务。", started_at, failure_reason="cancelled")
        job["events"].append(cancel_event_payload)
        if len(job["events"]) > RUN_JOB_MAX_EVENTS:
            overflow = len(job["events"]) - RUN_JOB_MAX_EVENTS
            del job["events"][:overflow]
            job["event_offset"] = int(job.get("event_offset") or 0) + overflow
    if cancel_event_payload and log_path:
        try:
            with Path(log_path).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(redact_sensitive(cancel_event_payload), ensure_ascii=False) + "\n")
        except Exception:
            pass
    return {"ok": True, "run_id": run_id, "status": "cancelled"}


@app.get("/api/long-link/events/{run_id}")
def long_link_job_events(run_id: str, request: Request, cursor: int = 0) -> dict[str, Any]:
    require_local_run_key(request)
    cleanup_run_jobs()
    with RUN_JOBS_LOCK:
        job = RUN_JOBS.get(run_id)
        if job is None:
            raise HTTPException(status_code=404, detail="运行编号不存在或已过期")
        events = list(job["events"])
        event_offset = int(job.get("event_offset") or 0)
        done = bool(job["done"])
        log_path = str(job["log_path"])
        started_at = float(job.get("started_at") or job.get("created_at") or time.time())
        now = time.time()
        heartbeat_due = not done and cursor >= event_offset + len(events) and now - float(job.get("last_heartbeat") or 0) >= 1.5
        if heartbeat_due:
            job["last_heartbeat"] = now
    requested_cursor = max(cursor, 0)
    window_shifted = requested_cursor < event_offset
    next_cursor = max(requested_cursor, event_offset)
    new_events = events[next_cursor - event_offset:]
    next_cursor += len(new_events)
    if window_shifted:
        new_events.insert(
            0,
            {
                "type": "log",
                "run_id": run_id,
                "step": "events",
                "message": "事件窗口已滚动，已跳到最近日志；完整日志仍保存在 ndjson 文件。",
                "ts": time.time(),
                "elapsed_ms": int((time.time() - started_at) * 1000),
            },
        )
    if heartbeat_due:
        now = time.time()
        new_events.append(
            {
                "type": "heartbeat",
                "run_id": run_id,
                "step": "heartbeat",
                "message": "仍在执行当前步骤...",
                "ts": now,
                "elapsed_ms": int((now - started_at) * 1000),
            }
        )
    return {
        "run_id": run_id,
        "cursor": next_cursor,
        "done": done,
        "log_path": log_path,
        "event_offset": event_offset,
        "events": new_events,
    }


def run_pp_zero_paypal_flow(req: LongLinkRequest, emit: Any | None = None) -> LongLinkResponse:
    """PP 0 元订阅提链主链路。

    固定执行：precheck_ip -> create_checkout -> 右池优惠/税区同步 ->
    stripe_init -> stripe_create_payment_method -> stripe_confirm ->
    chatgpt_approve -> pm-redirects -> PayPal BA approve。
    """

    def log(step: str, message: str, **extra: Any) -> None:
        if emit:
            emit(step, message, **extra)

    checkout_country = effective_country(req)
    pm_country = effective_payment_method_country(req)
    promo_country = effective_promotion_country(req)
    provider_country = provider_stage_country(req)
    flow_type = "paypal_zero_usd"
    log(
        "start",
        "开始执行 PP 0元订阅提链链路：左池 checkout → 右池 promotion/taxes → 左池 Stripe/approve → 右池 poll/redirect。",
    )
    log(
        "precheck_ip",
        f"Step 1 precheck_ip：左池 checkout/provider/approve={checkout_stage_country(req)}，右池 update/poll={promotion_stage_country(req)}。",
    )
    if chain_parent_proxy(req):
        log("precheck_ip", "本地链式代理已启用：本程序 → Clash → 住宅代理。")
    log(
        "create_checkout",
        f"Step 2 create_checkout：向 ChatGPT 网关创建 checkout，账单国家={checkout_country}，PM账单={pm_country}，优惠/税区={promo_country}。",
    )
    run_req, chatgpt, checkout = create_checkout_with_retry(req, emit=log)
    log("create_checkout", f"Step 2 create_checkout 完成：{checkout['cs_id']} / {checkout['billing_country']} / {checkout['currency']}。")
    provider = create_provider_link_with_retry(chatgpt, checkout, run_req, emit=log)
    stripe_hosted_url = provider["stripe_hosted_url"]
    hosted_long_url = to_openai_pay_url(stripe_hosted_url)
    log("done", "PP 0元订阅 BA 链提取完成。")

    return LongLinkResponse(
        ok=True,
        cs_id=checkout["cs_id"],
        processor_entity=checkout["processor_entity"],
        billing_country=checkout["billing_country"],
        payment_method_country=pm_country,
        promotion_country=promo_country,
        currency=checkout["currency"],
        payment_locale=locale_parts(run_req.payment_locale)[0],
        flow_type=flow_type,
        payment_method_type="paypal",
        payment_method_id=provider["payment_method_id"],
        stripe_redirect_url=provider["stripe_redirect_url"],
        provider_redirect_url=provider["provider_redirect_url"],
        fallback=False,
        provider_error="",
        stripe_hosted_url=stripe_hosted_url,
        long_url=provider["long_url"] or hosted_long_url,
        stripe_amount=provider.get("stripe_amount", ""),
        stripe_amount_source=provider.get("stripe_amount_source", ""),
        link_attempt_count=int(str(provider.get("link_attempt_count") or 0) or 0),
        link_attempt_limit=int(str(provider.get("link_attempt_limit") or getattr(run_req, "provider_retry_limit", 0) or 0) or 0),
    )


def generate_long_link_payload(req: LongLinkRequest, emit: Any | None = None) -> LongLinkResponse:
    def log(step: str, message: str, **extra: Any) -> None:
        if emit:
            emit(step, message, **extra)

    if str(getattr(req, "run_mode", "") or "").strip().lower() in {"pp_zero_paypal", "paypal_zero", "pp_zero_subscription"}:
        return run_pp_zero_paypal_flow(req, emit=emit)

    fixed_mode_country = paypal_country_mode_country(req)
    if fixed_mode_country:
        req = req.model_copy(update={"billing_country": fixed_mode_country, "payment_method_country": fixed_mode_country})
    requested_checkout = effective_country(req)
    requested_pm = effective_payment_method_country(req)
    combos = [(requested_checkout, requested_pm)] if str(req.batch_target_country or "").strip() else combo_attempt_order(requested_checkout, requested_pm)
    failures: list[str] = []
    combo_results: list[dict[str, str]] = []
    log("start", "开始执行 PP 链提取流程，已启用组合回退。")

    for index, (checkout_country, pm_country) in enumerate(combos, start=1):
        combo_started_at = time.time()
        combo_label = combo_name(checkout_country, pm_country)
        combo_result = {"combo": combo_label, "status": "运行中", "detail": ""}
        combo_results.append(combo_result)
        combo_req = req.model_copy(update={"billing_country": checkout_country, "payment_method_country": pm_country})
        log(
            "combo",
            f"尝试组合 {index}/{len(combos)}：checkout={checkout_country}/{currency_for_country(checkout_country)}，PM={pm_country}，provider={provider_stage_country(combo_req)}。",
        )
        try:
            result = run_single_combo(
                combo_req,
                checkout_country,
                pm_country,
                fallback=combo_label != combo_name(requested_checkout, requested_pm),
                prior_failures=failures,
                emit=emit,
            )
            if result.fallback:
                log("combo", f"组合 {combo_label} 回退成功。")
            combo_result["status"] = "成功"
            combo_result["elapsed_ms"] = str(int((time.time() - combo_started_at) * 1000))
            emit_combo_result(
                emit,
                index,
                checkout_country,
                pm_country,
                "成功",
                int(combo_result["elapsed_ms"]),
                checkout_stage_country(combo_req),
                provider_stage_country(combo_req),
            )
            log("summary", format_combo_results(combo_results))
            return result
        except HTTPException as exc:
            detail = str(exc.detail)
            short_detail = short_error(detail)
            combo_result["status"] = "失败"
            combo_result["detail"] = summarize_combo_failure(detail)
            summary = f"{combo_label}: {combo_result['detail'] or short_detail}"
            failures.append(summary)
            combo_result["elapsed_ms"] = str(int((time.time() - combo_started_at) * 1000))
            emit_combo_result(
                emit,
                index,
                checkout_country,
                pm_country,
                "失败",
                int(combo_result["elapsed_ms"]),
                checkout_stage_country(combo_req),
                provider_stage_country(combo_req),
                combo_result["detail"],
            )
            if not is_retryable_combo_failure(exc):
                log("error", f"组合 {combo_label} 失败且不可回退：{short_detail}")
                log("summary", format_combo_results(combo_results))
                raise
            log("combo", f"组合 {combo_label} 未拿到 BA approve：{short_detail}")
            if index < len(combos):
                next_checkout, next_pm = combos[index]
                log("combo", f"自动切换下一个组合：checkout={next_checkout}，PM={next_pm}。")
        except Exception as exc:
            if not is_retryable_network_error(exc):
                raise
            detail = f"provider 网络异常: {exc}"
            short_detail = short_error(detail)
            summary = f"{combo_label}: {short_detail}"
            failures.append(summary)
            combo_result["status"] = "失败"
            combo_result["detail"] = "provider 网络异常"
            combo_result["elapsed_ms"] = str(int((time.time() - combo_started_at) * 1000))
            emit_combo_result(
                emit,
                index,
                checkout_country,
                pm_country,
                "失败",
                int(combo_result["elapsed_ms"]),
                checkout_stage_country(combo_req),
                provider_stage_country(combo_req),
                combo_result["detail"],
            )
            log("combo", f"组合 {combo_label} 网络异常，进入下一个组合：{short_detail}")
            if index < len(combos):
                next_checkout, next_pm = combos[index]
                log("combo", f"自动切换下一个组合：checkout={next_checkout}，PM={next_pm}。")

    last_detail = failures[-1] if failures else "没有可用组合"
    log("summary", format_combo_results(combo_results))
    raise HTTPException(status_code=502, detail=f"所有组合均未提取到 PayPal BA approve 链；{'; '.join(failures) or last_detail}")


def combo_name(checkout_country: str, pm_country: str) -> str:
    return f"{checkout_country}+{pm_country}"


def format_combo_results(combo_results: list[dict[str, str]]) -> str:
    compact: list[str] = []
    detailed: list[str] = []
    for item in combo_results:
        combo = item.get("combo") or "未知组合"
        status = item.get("status") or "未知"
        detail = item.get("detail") or ""
        compact.append(f"{combo} {status}")
        detailed.append(f"{combo} {status}{'：' + detail if detail and status == '失败' else ''}")
    if not compact:
        return "组合结果：无已尝试组合"
    if compact == detailed:
        return "组合结果：" + "；".join(compact)
    return "组合结果：" + "；".join(compact) + "。组合详情：" + "；".join(detailed)


def emit_combo_result(
    emit: Any | None,
    index: int,
    checkout_country: str,
    pm_country: str,
    status: str,
    elapsed_ms: int,
    checkout_proxy_country: str = "JP",
    provider_proxy_country: str = "US",
    detail: str = "",
) -> None:
    if not emit:
        return
    status_text = "成功" if status == "成功" else "失败"
    checkout_proxy_country = str(checkout_proxy_country or "JP").upper()
    provider_proxy_country = str(provider_proxy_country or "US").upper()
    message = (
        f"组合测试：{checkout_country} / {pm_country} / {currency_for_country(checkout_country)} / "
        f"en-US / {checkout_proxy_country} / {provider_proxy_country}"
    )
    emit(
        "combo_result",
        message,
        combo_index=index,
        combo=combo_name(checkout_country, pm_country),
        checkout_country=checkout_country,
        pm_country=pm_country,
        currency=currency_for_country(checkout_country),
        locale="en-US",
        checkout_proxy_country=checkout_proxy_country,
        provider_proxy_country=provider_proxy_country,
        status=status_text,
        detail=detail,
        combo_elapsed_ms=elapsed_ms,
    )


def summarize_combo_failure(detail: str) -> str:
    text = str(detail or "")
    markers = [
        r"confirm_error_reason=([^,，;；]+)",
        r"confirm_error_code=([^,，;；]+)",
        r"submission_reason=([^,，;；]+)",
        r"submission_code=([^,，;；]+)",
        r"submission_message=([^,，;；]+)",
        r"reason=([^,，;；]+)",
        r"code=([^,，;；]+)",
        r"message=([^,，;；]+)",
    ]
    for marker in markers:
        match = re.search(marker, text)
        if match:
            value = match.group(1).strip()
            if value and value.lower() not in {"无", "none", "unknown", "未知"}:
                return short_error(value, 80)
    lowered = text.lower()
    if "blocked/exception burst failed" in lowered:
        return "approve blocked/exception 复投失败"
    if "chatgpt approve retryable result" in lowered:
        return "approve blocked/exception，已尝试复投"
    if "chatgpt approve unexpected result" in lowered:
        return short_error(text.replace("chatgpt approve unexpected result:", "approve"), 80)
    if "provider US 代理检测未通过" in text or "代理不能为空" in text:
        return "代理检测失败"
    if "provider 网络异常" in text:
        return "provider 网络异常"
    if "redirect url resolution timeout" in text:
        return "未解析到 BA approve 链"
    if "stripe submission failed" in text:
        return "Stripe submission failed"
    if "未提取到最终 PayPal BA approve 链" in text:
        return "未提取到 BA approve 链"
    return short_error(text, 80)


def combo_attempt_order(checkout_country: str, pm_country: str) -> list[tuple[str, str]]:
    requested = (normalize_country(checkout_country), normalize_country(pm_country))
    ordered = [requested]
    if requested == ("DE", "DE"):
        ordered.extend([("US", "US"), ("DE", "US"), ("US", "DE")])
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in ordered:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def short_error(detail: str, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", str(detail or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def is_retryable_combo_failure(exc: HTTPException) -> bool:
    detail = str(exc.detail or "").lower()
    if "payment_method_types_mismatch" in detail:
        return True
    if exc.status_code in (400, 401, 403):
        return False
    non_retryable_markers = (
        "access token",
        "unauthorized",
        "forbidden",
        "invalid api key",
        "invalid publishable key",
        "provider us 代理检测未通过",
        "jp 代理检测失败",
        "代理检测未通过",
    )
    if any(marker in detail for marker in non_retryable_markers):
        return False
    retryable_markers = (
        "ba approve",
        "redirect url resolution timeout",
        "stripe submission failed",
        "submission_attempt",
        "stripe 资源 url",
        "未提取到最终 paypal",
        "paypal url",
    )
    return exc.status_code in (502, 504) or any(marker in detail for marker in retryable_markers)


def provider_retry_limit(req: LongLinkRequest) -> int:
    return max(1, min(20, int(getattr(req, "provider_retry_limit", PROXY_EXIT_RETRY_ATTEMPTS) or PROXY_EXIT_RETRY_ATTEMPTS)))


def run_strict_sticky_combo(
    req: LongLinkRequest,
    checkout_country: str,
    pm_country: str,
    fallback: bool,
    prior_failures: list[str],
    emit: Any | None = None,
) -> LongLinkResponse:
    def log(step: str, message: str, **extra: Any) -> None:
        if emit:
            emit(step, message, **extra)

    checkout_country = effective_country(req)
    pm_countries = payment_method_country_pool(req)
    pm_country = pm_countries[0]
    flow_type = "paypal_de" if checkout_country == "DE" else "paypal_jp"
    provider_country = provider_stage_country(req)
    max_attempts = provider_retry_limit(req)
    attempt_failures: list[str] = []
    last_error = ""

    log(
        "billing",
        f"当前组合：checkout账单={checkout_country}/{currency_for_country(checkout_country)}，"
        f"PM账单轮询={','.join(pm_countries)}，左池={provider_country}，右池={promotion_stage_country(req)}。",
    )
    log("proxy", "双池严格模式：每轮重建 checkout；左池 checkout/provider/approve 共用 sticky IP，右池 update/poll 独立轮换。")

    used_checkout_bases: set[str] = set()
    for attempt in range(1, max_attempts + 1):
        ensure_not_cancelled()
        attempt_pm_country = pm_countries[(attempt - 1) % len(pm_countries)]
        run_req = request_with_rotated_checkout_session(req.model_copy(update={"payment_method_country": attempt_pm_country}), used_bases=used_checkout_bases)
        checkout_country_label = checkout_stage_country(run_req)
        log(
            "link_attempt",
            f"提链尝试 {attempt}/{max_attempts}：PM账单={attempt_pm_country}，新建 checkout 并更换 {checkout_country_label} sticky IP。",
            link_attempt_count=attempt,
            link_attempt_limit=max_attempts,
            provider_country=provider_country,
        )
        log("proxy", f"第 {attempt}/{max_attempts} 轮：已生成新的 {checkout_country_label} sticky session，checkout/provider 共用。")
        checkout_proxy_text = "统一代理" if single_proxy_mode(run_req) else f"{checkout_country_label} 代理"
        try:
            log("checkout", f"正在使用 {checkout_proxy_text} 创建 ChatGPT checkout。")
            run_req, chatgpt, checkout = create_checkout_with_retry(run_req, emit=log)
            log("checkout", f"checkout 创建成功：{checkout['cs_id']} / {checkout['billing_country']} / {checkout['currency']}。")
            stripe_key_source = (
                "手动填写"
                if run_req.stripe_publishable_key.strip()
                else ("checkout 响应" if checkout.get("stripe_publishable_key") else "内置默认")
            )
            log("stripe_init", f"Stripe key 来源：{stripe_key_source}，checkout_ui_mode=custom。")
            log("provider", f"正在复用 checkout sticky IP 执行 provider：{provider_country}；approve 阶段不强制同 IP。")
            provider = create_provider_link_with_retry(
                chatgpt,
                checkout,
                run_req,
                emit=log,
                max_attempts_override=1,
                attempt_offset=attempt - 1,
                attempt_limit=max_attempts,
            )
            stripe_hosted_url = provider["stripe_hosted_url"]
            hosted_long_url = to_openai_pay_url(stripe_hosted_url)
            log("done", "PP 链提取完成。")

            return LongLinkResponse(
                ok=True,
                cs_id=checkout["cs_id"],
                processor_entity=checkout["processor_entity"],
                billing_country=checkout["billing_country"],
                payment_method_country=attempt_pm_country,
                promotion_country=promotion_stage_country(run_req),
                currency=checkout["currency"],
                payment_locale=locale_parts(run_req.payment_locale)[0],
                flow_type=flow_type,
                payment_method_type="paypal",
                payment_method_id=provider["payment_method_id"],
                stripe_redirect_url=provider["stripe_redirect_url"],
                provider_redirect_url=provider["provider_redirect_url"],
                fallback=fallback,
                provider_error="; ".join(prior_failures + attempt_failures),
                stripe_hosted_url=stripe_hosted_url,
                long_url=provider["long_url"] or hosted_long_url,
                stripe_amount=provider.get("stripe_amount", ""),
                stripe_amount_source=provider.get("stripe_amount_source", ""),
                link_attempt_count=int(str(provider.get("link_attempt_count") or attempt) or 0),
                link_attempt_limit=int(str(provider.get("link_attempt_limit") or max_attempts) or 0),
            )
        except HTTPException as exc:
            if not is_retryable_provider_flow_exception(exc):
                raise
            last_error = str(exc.detail or exc)
            attempt_failures.append(short_error(last_error))
            log(
                "link_attempt",
                f"提链尝试 {attempt}/{max_attempts} 失败：{short_error(last_error)}",
                link_attempt_count=attempt,
                link_attempt_limit=max_attempts,
                provider_country=provider_country,
            )
            if attempt < max_attempts:
                log("proxy", f"丢弃本轮 checkout，{PROXY_EXIT_RETRY_DELAY_SECONDS}秒后换新 sticky IP 重开。")
                cancellable_sleep(PROXY_EXIT_RETRY_DELAY_SECONDS)
                continue
            break
        except Exception as exc:
            if not is_retryable_network_error(exc):
                raise
            last_error = str(exc)
            attempt_failures.append(short_error(last_error))
            log(
                "link_attempt",
                f"提链尝试 {attempt}/{max_attempts} 网络异常：{short_error(last_error)}",
                link_attempt_count=attempt,
                link_attempt_limit=max_attempts,
                provider_country=provider_country,
            )
            if attempt < max_attempts:
                log("proxy", f"丢弃本轮 checkout，{PROXY_EXIT_RETRY_DELAY_SECONDS}秒后换新 sticky IP 重开。")
                cancellable_sleep(PROXY_EXIT_RETRY_DELAY_SECONDS)
                continue
            break

    raise HTTPException(
        status_code=504,
        detail=f"严格 sticky 提链已达到上限 {max_attempts} 轮仍失败: {short_error(last_error)}",
    )


def run_single_combo(
    req: LongLinkRequest,
    checkout_country: str,
    pm_country: str,
    fallback: bool,
    prior_failures: list[str],
    emit: Any | None = None,
) -> LongLinkResponse:
    if bool(getattr(req, "strict_same_proxy_ip", True)) and provider_uses_checkout(req):
        return run_strict_sticky_combo(req, checkout_country, pm_country, fallback, prior_failures, emit=emit)

    def log(step: str, message: str, **extra: Any) -> None:
        if emit:
            emit(step, message, **extra)

    checkout_country = effective_country(req)
    pm_country = effective_payment_method_country(req)
    flow_type = "paypal_de" if checkout_country == "DE" else "paypal_jp"
    provider_country = provider_stage_country(req)
    provider_label = provider_stage_label(req)
    log(
        "billing",
        f"当前组合：checkout账单={checkout_country}/{currency_for_country(checkout_country)}，"
        f"PM账单={pm_country}，provider代理={provider_country}。",
    )
    log("proxy", f"检测 {provider_label} 出口；checkout/approve 会在对应阶段检测。")
    run_req = req
    proxy_error = ""
    used_provider_bases: set[str] = set()
    for attempt in range(1, 6):
        ensure_not_cancelled()
        rotated_proxy = provider_stage_proxy(req) if single_proxy_mode(req) else rotated_stage_proxy_for_attempt(req, "provider", provider_country, provider_stage_proxy(req), used_bases=used_provider_bases)
        run_req = request_with_stage_proxy(req, "provider", rotated_proxy, provider_country)
        session_text = "复用统一代理" if single_proxy_mode(req) else "自动轮换粘性 session"
        log("proxy", f"{provider_label} 第 {attempt}/5 次：{session_text}。")
        provider_probe = check_provider_proxy(run_req)
        if provider_probe.ok:
            break
        proxy_error = provider_probe.error or f"{provider_label} 代理不可用"
        log("proxy", f"{provider_label} 不可用，准备更换：{proxy_error}")
    else:
        raise HTTPException(status_code=400, detail=f"{provider_label} 代理检测未通过: {proxy_error}")
    log("proxy", f"provider 代理检测通过：{format_proxy_probe_summary([provider_probe])}。")
    checkout_proxy_text = "统一代理" if single_proxy_mode(run_req) else f"{checkout_stage_country(run_req)} 代理"
    log("checkout", f"正在使用 {checkout_proxy_text} 创建 ChatGPT checkout。")
    run_req, chatgpt, checkout = create_checkout_with_retry(run_req, emit=log)
    log("checkout", f"checkout 创建成功：{checkout['cs_id']} / {checkout['billing_country']} / {checkout['currency']}。")
    stripe_key_source = (
        "手动填写"
        if run_req.stripe_publishable_key.strip()
        else ("checkout 响应" if checkout.get("stripe_publishable_key") else "内置默认")
    )
    log("stripe_init", f"Stripe key 来源：{stripe_key_source}；checkout_ui_mode=custom。")
    log("provider", f"正在切换到 provider 阶段代理：{provider_country}。")
    provider = create_provider_link_with_retry(chatgpt, checkout, run_req, emit=log)
    stripe_hosted_url = provider["stripe_hosted_url"]
    hosted_long_url = to_openai_pay_url(stripe_hosted_url)
    log("done", "PP 链提取完成。")

    return LongLinkResponse(
        ok=True,
        cs_id=checkout["cs_id"],
        processor_entity=checkout["processor_entity"],
        billing_country=checkout["billing_country"],
        payment_method_country=pm_country,
        currency=checkout["currency"],
        payment_locale=locale_parts(run_req.payment_locale)[0],
        flow_type=flow_type,
        payment_method_type="paypal",
        payment_method_id=provider["payment_method_id"],
        stripe_redirect_url=provider["stripe_redirect_url"],
        provider_redirect_url=provider["provider_redirect_url"],
        fallback=fallback,
        provider_error="; ".join(prior_failures),
        stripe_hosted_url=stripe_hosted_url,
        long_url=provider["long_url"] or hosted_long_url,
        stripe_amount=provider.get("stripe_amount", ""),
        stripe_amount_source=provider.get("stripe_amount_source", ""),
        link_attempt_count=int(str(provider.get("link_attempt_count") or 0) or 0),
        link_attempt_limit=int(str(provider.get("link_attempt_limit") or getattr(run_req, "provider_retry_limit", 0) or 0) or 0),
    )
