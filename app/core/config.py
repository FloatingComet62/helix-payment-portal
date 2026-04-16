from functools import lru_cache
from decimal import Decimal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # MongoDB
    MONGODB_URI: str = "mongodb://localhost:27017"
    MONGODB_DB_NAME: str = "central_payment"
    MONGO_MAX_POOL_SIZE: int = 50   # per pod; 2 pods = 100 total Atlas connections
    MONGO_MIN_POOL_SIZE: int = 10   # warm connections kept alive to avoid TLS cold-start

    # Redis
    REDIS_URL: str = "redis://localhost:6379"

    # EaseBuzz
    EASEBUZZ_KEY: str
    EASEBUZZ_SALT: str
    EASEBUZZ_ENV: str = "test"  # "test" | "prod"

    # Portal
    PORTAL_CALLBACK_URL: str  # Our server's URL EaseBuzz POSTs the S2S webhook to

    # CORS
    CORS_ALLOW_ORIGINS: str = (
        # "http://localhost:3000,"
        # "http://127.0.0.1:3000,"
        "https://accommodationstiet.shop,"
        "https://www.accommodationstiet.shop,"
        # "https://thapar-outreach-payment-ui.vercel.app"
    )
    CORS_ALLOW_ORIGIN_REGEX: str = ""

    # Admin
    ADMIN_API_KEY: str

    # LHS report key
    LHS_API_KEY: str = "DEFAULT"

    # Fee config
    EASEBUZZ_FEE_PERCENT: Decimal = Decimal("2.00")

    @property
    def easebuzz_base_url(self) -> str:
        return "https://pay.easebuzz.in" if self.EASEBUZZ_ENV == "prod" else "https://testpay.easebuzz.in"

    @property
    def easebuzz_payment_url(self) -> str:
        return f"{self.easebuzz_base_url}/payment/initiateLink"

    @property
    def easebuzz_refund_url(self) -> str:
        return f"{self.easebuzz_base_url}/transaction/refund/v2"

    @property
    def easebuzz_transaction_url(self) -> str:
        return f"{self.easebuzz_base_url}/transaction/v2"

    @property
    def cors_allow_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ALLOW_ORIGINS.split(",") if origin.strip()]

    @property
    def cors_allow_origin_regex(self) -> str | None:
        regex = self.CORS_ALLOW_ORIGIN_REGEX.strip()
        return regex or None


@lru_cache
def get_settings() -> Settings:
    return Settings()
