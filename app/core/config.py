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

    # EaseBuzz
    EASEBUZZ_KEY: str
    EASEBUZZ_SALT: str
    EASEBUZZ_ENV: str = "test"  # "test" | "prod"

    # Portal
    PORTAL_CALLBACK_URL: str  # Our server's URL EaseBuzz POSTs the S2S webhook to

    # Admin
    ADMIN_API_KEY: str

    # Fee config
    EASEBUZZ_FEE_PERCENT: Decimal = Decimal("2.00")

    @property
    def easebuzz_payment_url(self) -> str:
        if self.EASEBUZZ_ENV == "prod":
            return "https://pay.easebuzz.in/payment/initiateLink"
        return "https://testpay.easebuzz.in/payment/initiateLink"


@lru_cache
def get_settings() -> Settings:
    return Settings()
