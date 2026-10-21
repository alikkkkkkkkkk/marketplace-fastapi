from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import User
from .security import hash_password


def bootstrap_admin() -> None:
    with SessionLocal() as db, db.begin():
        existing = db.execute(select(User).where(User.email == settings.admin_email)).scalar_one_or_none()
        if existing is None:
            db.add(
                User(
                    email=settings.admin_email,
                    password_hash=hash_password(settings.admin_password),
                    role="ADMIN",
                )
            )
