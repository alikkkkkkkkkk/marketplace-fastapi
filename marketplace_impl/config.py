from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace",
    )
    jwt_secret: str = os.getenv("JWT_SECRET", "dev-secret-change-me-please-123456789")
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256")
    access_token_minutes: int = int(os.getenv("ACCESS_TOKEN_MINUTES", "20"))
    refresh_token_days: int = int(os.getenv("REFRESH_TOKEN_DAYS", "14"))
    order_operation_cooldown_minutes: int = int(
        os.getenv("ORDER_OPERATION_COOLDOWN_MINUTES", "1")
    )
    admin_email: str = os.getenv("ADMIN_EMAIL", "admin@example.com")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "Admin123!")


settings = Settings()
