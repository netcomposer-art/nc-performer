"""Environment configuration. Every limit is a setting. Nothing here is a secret
except the values of the key variables, which are never written to disk by this
module.

xAI (the hosted default, used only when a Composition needs a line, triplets,
or sections):

  XAI_API_KEY          bearer token for https://api.x.ai/v1
  NC_LLM_MODEL         default grok-4.3 when XAI_API_KEY is set

The kit posts to ``{NC_LLM_BASE_URL}/chat/completions``. If you set
NC_LLM_BASE_URL yourself, that host is used instead of api.x.ai and
NC_LLM_API_KEY (or XAI_API_KEY) is the bearer token. If neither a key nor a
base URL is set, the instance runs stub plans only.

Tests point NC_LLM_BASE_URL at a local mock. They never call api.x.ai.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


DEFAULT_HOSTS = (
    "musicmatter.art",
    # Live chord files are served here. The registry host is musicmatter.art.
    "audio.musicmatter.art",
    "fullchords.musicmatter.art",
    "netcomposer.art",
    "freesound.org",
    "cdn.freesound.org",
    "theremin.music.uiowa.edu",
    "webfiles.york.ac.uk",
    "www.openair.hosted.york.ac.uk",
    "web.archive.org",
    "archive.org",
    "us.archive.org",
    "api.openverse.org",
    "commons.wikimedia.org",
    "upload.wikimedia.org",
    "wikimedia.org",
    "ccmixter.org",
    "api.x.ai",
)

# grok-4.3 list prices, USD per token. Override when you point at another model.
# https://docs.x.ai/docs/models (read for the plan on 18 Oct 2026): $1.25 / $0.20 cached / $2.50 per 1M.
DEFAULT_LLM_USD = (1.25 / 1_000_000, 0.20 / 1_000_000, 2.50 / 1_000_000)
# Cloudflare Containers standard-3 active-use rates from the hosted plan.
DEFAULT_CPU_USD_S = 0.000020
DEFAULT_GIB_USD_S = 0.0000025


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


@dataclass
class Settings:
    storage: str = "file:///data"
    public_base_url: str = "http://localhost:8080"
    instance_name: str = "nc-perform (self-hosted)"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    xai_api_key: str = ""
    turnstile_sitekey: str = ""
    turnstile_secret: str = ""
    turnstile_verify_url: str = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    signing_key: str = ""
    suppliers_json: str = "https://netcomposer.art/suppliers.json"
    max_length_s: float = 180
    max_concurrency: int = 3
    rate_per_10min: int = 3
    rate_per_ip_day: int = 10
    max_queue: int = 50
    max_sources: int = 24
    max_chords: int = 40
    max_layers: int = 16
    max_words: int = 4000
    max_plan_bytes: int = 64 * 1024
    max_submit_bytes: int = 50 * 1024 * 1024
    job_timeout_s: int = 600
    daily_soft_usd: float = 3.0
    daily_hard_usd: float = 5.0
    allowed_hosts: tuple[str, ...] = DEFAULT_HOSTS
    perform_enabled: bool = True
    admin_token: str = ""
    tester_token: str = ""
    internal_token: str = ""
    ip_hash_salt: str = "nc-perform"
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "auto"
    s3_unsigned: bool = False
    bind: str = "0.0.0.0:8080"
    role: str = "all"  # all | container
    queue_autostart: bool = True
    data_dir: str = "/data"
    audio_retain_days: int = 90
    failed_retain_days: int = 7
    public_delay_hours: int = 24
    provisioned_gib: float = 8.0
    llm_usd_in: float = DEFAULT_LLM_USD[0]
    llm_usd_cached: float = DEFAULT_LLM_USD[1]
    llm_usd_out: float = DEFAULT_LLM_USD[2]
    llm_every_perform: bool = False
    cpu_usd_s: float = DEFAULT_CPU_USD_S
    gib_usd_s: float = DEFAULT_GIB_USD_S
    extra_hosts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)

    @property
    def turnstile_required(self) -> bool:
        return bool(self.turnstile_secret)

    def host_allowed(self, host: str | None) -> bool:
        if not host:
            return False
        h = host.lower().rstrip(".")
        allowed = set(self.allowed_hosts) | set(self.extra_hosts)
        sup = _host_of(self.suppliers_json)
        if sup:
            allowed.add(sup)
        if self.llm_base_url:
            llm = _host_of(self.llm_base_url)
            if llm:
                allowed.add(llm)
        return any(h == a or h.endswith("." + a) for a in allowed)


def _host_of(url: str) -> str:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "").lower()


def tester_pass(header: str | None, secret: str | None) -> bool:
    """True when X-NC-Tester matches NC_TESTER_TOKEN.

    An empty secret never matches. Both sides are hashed before the compare so
    a short header cannot skip it, and the token is not returned or logged.
    """
    import hashlib
    import hmac

    token = (secret or "").strip()
    got = (header or "").strip()
    if not token or not got:
        return False
    return hmac.compare_digest(hashlib.sha256(got.encode()).digest(), hashlib.sha256(token.encode()).digest())


def load() -> Settings:
    """Read process environment. Safe to call more than once."""
    xai = os.environ.get("XAI_API_KEY", "").strip()
    base = os.environ.get("NC_LLM_BASE_URL", "").strip()
    model = os.environ.get("NC_LLM_MODEL", "").strip()
    key = os.environ.get("NC_LLM_API_KEY", "").strip()
    # XAI_API_KEY alone selects the documented hosted default. An explicit
    # NC_LLM_BASE_URL wins, so a self-hoster can point at any OpenAI-compatible
    # server (or a local mock) without unsetting a leftover key.
    if xai and not base:
        base = "https://api.x.ai/v1"
    if xai and not model and (not base or "api.x.ai" in base):
        model = "grok-4.3"
    if not key:
        key = xai
    storage = os.environ.get("NC_STORAGE", "file:///data")
    data_dir = os.environ.get("NC_DATA_DIR", "")
    if not data_dir:
        if storage.startswith("file://"):
            data_dir = storage[len("file://"):] or "/data"
        else:
            data_dir = os.environ.get("NC_DATA_DIR", "/data")
    enabled = os.environ.get("PERFORM_ENABLED", "1").strip().lower() not in ("0", "false", "off", "no")
    s = Settings(
        storage=storage,
        public_base_url=os.environ.get("NC_PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/"),
        instance_name=os.environ.get("NC_INSTANCE_NAME", "nc-perform (self-hosted)"),
        llm_base_url=base.rstrip("/"),
        llm_model=model,
        llm_api_key=key,
        xai_api_key=xai,
        turnstile_sitekey=os.environ.get("TURNSTILE_SITEKEY", "").strip(),
        turnstile_secret=os.environ.get("TURNSTILE_SECRET", os.environ.get("TURNSTILE_SECRET_KEY", "")).strip(),
        turnstile_verify_url=os.environ.get("TURNSTILE_VERIFY_URL", "https://challenges.cloudflare.com/turnstile/v0/siteverify"),
        signing_key=os.environ.get("NC_SIGNING_KEY", ""),
        suppliers_json=os.environ.get("NC_SUPPLIERS_JSON", "https://netcomposer.art/suppliers.json"),
        max_length_s=_f("NC_MAX_LENGTH_S", 180),
        max_concurrency=_i("NC_MAX_CONCURRENCY", 3),
        rate_per_10min=_i("NC_RATE_PER_10MIN", 3),
        rate_per_ip_day=_i("NC_RATE_PER_IP_DAY", 10),
        max_queue=_i("NC_MAX_QUEUE", 50),
        max_sources=_i("NC_MAX_SOURCES", 24),
        max_chords=_i("NC_MAX_CHORDS", 40),
        max_layers=_i("NC_MAX_LAYERS", 16),
        max_words=_i("NC_MAX_WORDS", 4000),
        job_timeout_s=_i("NC_JOB_TIMEOUT_S", 600),
        daily_soft_usd=_f("NC_DAILY_BUDGET_USD", 3.0),
        daily_hard_usd=_f("NC_DAILY_BUDGET_HARD_USD", 5.0),
        allowed_hosts=_csv("NC_ALLOWED_HOSTS", DEFAULT_HOSTS),
        perform_enabled=enabled,
        admin_token=os.environ.get("NC_ADMIN_TOKEN", ""),
        tester_token=os.environ.get("NC_TESTER_TOKEN", "").strip(),
        internal_token=os.environ.get("NC_INTERNAL_TOKEN", ""),
        ip_hash_salt=os.environ.get("NC_IP_HASH_SALT", "nc-perform"),
        s3_endpoint=os.environ.get("NC_S3_ENDPOINT", "").rstrip("/"),
        s3_access_key=os.environ.get("NC_S3_ACCESS_KEY_ID", os.environ.get("AWS_ACCESS_KEY_ID", "")),
        s3_secret_key=os.environ.get("NC_S3_SECRET_ACCESS_KEY", os.environ.get("AWS_SECRET_ACCESS_KEY", "")),
        s3_region=os.environ.get("NC_S3_REGION", "auto"),
        s3_unsigned=os.environ.get("NC_S3_UNSIGNED", "").strip() in ("1", "true", "yes"),
        bind=os.environ.get("NC_BIND", "0.0.0.0:8080"),
        role=os.environ.get("NC_ROLE", "all"),
        queue_autostart=os.environ.get("NC_QUEUE_AUTOSTART", "1").strip() not in ("0", "false", "no"),
        data_dir=data_dir,
        audio_retain_days=_i("NC_AUDIO_RETAIN_DAYS", 90),
        failed_retain_days=_i("NC_FAILED_RETAIN_DAYS", 7),
        public_delay_hours=_i("NC_PUBLIC_DELAY_HOURS", 24),
        llm_usd_in=_f("NC_LLM_USD_PER_TOKEN_IN", DEFAULT_LLM_USD[0]),
        llm_usd_cached=_f("NC_LLM_USD_PER_TOKEN_CACHED", DEFAULT_LLM_USD[1]),
        llm_usd_out=_f("NC_LLM_USD_PER_TOKEN_OUT", DEFAULT_LLM_USD[2]),
        llm_every_perform=os.environ.get("NC_LLM_EVERY_PERFORM", "").strip().lower() in ("1", "true", "yes", "on"),
        cpu_usd_s=_f("NC_CPU_USD_PER_S", DEFAULT_CPU_USD_S),
        gib_usd_s=_f("NC_GIB_USD_PER_S", DEFAULT_GIB_USD_S),
    )
    return s
