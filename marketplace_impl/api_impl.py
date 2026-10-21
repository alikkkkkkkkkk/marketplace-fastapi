from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from starlette.responses import Response

from marketplace_api.apis.auth_api_base import BaseAuthApi
from marketplace_api.apis.orders_api_base import BaseOrdersApi
from marketplace_api.apis.products_api_base import BaseProductsApi
from marketplace_api.apis.promo_codes_api_base import BasePromoCodesApi

from .config import settings
from .db import SessionLocal
from .errors import ApiException
from .models import Order, OrderItem, Product, PromoCode, User, UserOperation
from .security import (
    create_access_token,
    create_refresh_token,
    get_current_principal,
    hash_password,
    rotate_refresh_token,
    verify_password,
)

MONEY = Decimal("0.01")


def enum_value(value):
    return value.value if hasattr(value, "value") else value


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def require_role(*roles: str):
    principal = get_current_principal()
    if principal.role not in roles:
        raise ApiException(403, "ACCESS_DENIED", "Insufficient permissions")
    return principal


def product_dict(p: Product) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "price": p.price,
        "stock": p.stock,
        "category": p.category,
        "status": p.status,
        "seller_id": p.seller_id,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def promo_dict(p: PromoCode) -> dict:
    return {
        "id": p.id,
        "code": p.code,
        "discount_type": p.discount_type,
        "discount_value": p.discount_value,
        "min_order_amount": p.min_order_amount,
        "max_uses": p.max_uses,
        "current_uses": p.current_uses,
        "valid_from": p.valid_from,
        "valid_until": p.valid_until,
        "active": p.active,
    }


def order_dict(db, order: Order) -> dict:
    items = db.execute(
        select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)
    ).scalars().all()
    return {
        "id": order.id,
        "user_id": order.user_id,
        "status": order.status,
        "promo_code_id": order.promo_code_id,
        "items": [
            {
                "id": item.id,
                "product_id": item.product_id,
                "quantity": item.quantity,
                "price_at_order": item.price_at_order,
            }
            for item in items
        ],
        "total_amount": order.total_amount,
        "discount_amount": order.discount_amount,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
    }


def _lock_user(db, user_id: uuid.UUID) -> User:
    user = db.execute(select(User).where(User.id == user_id).with_for_update()).scalar_one_or_none()
    if user is None:
        raise ApiException(401, "TOKEN_INVALID", "Token user no longer exists")
    return user


def _check_order_access(order: Order):
    principal = get_current_principal()
    if principal.role == "SELLER":
        raise ApiException(403, "ACCESS_DENIED", "SELLER cannot access orders")
    if principal.role != "ADMIN" and order.user_id != principal.user_id:
        raise ApiException(403, "ORDER_OWNERSHIP_VIOLATION", "Order belongs to another user")
    return principal


def _check_rate_limit(db, user_id: uuid.UUID, operation_type: str):
    cooldown = settings.order_operation_cooldown_minutes
    if cooldown <= 0:
        return
    last = db.execute(
        select(UserOperation)
        .where(
            UserOperation.user_id == user_id,
            UserOperation.operation_type == operation_type,
        )
        .order_by(UserOperation.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last and last.created_at > now_utc() - timedelta(minutes=cooldown):
        retry_after = max(
            0,
            int((last.created_at + timedelta(minutes=cooldown) - now_utc()).total_seconds()),
        )
        raise ApiException(
            429,
            "ORDER_LIMIT_EXCEEDED",
            "Order operation rate limit exceeded",
            {"retry_after_seconds": retry_after},
        )


def _normalized_items(items) -> dict[uuid.UUID, int]:
    merged: dict[uuid.UUID, int] = {}
    for item in items:
        product_id = uuid.UUID(str(item.product_id))
        merged[product_id] = merged.get(product_id, 0) + int(item.quantity)
        if merged[product_id] > 999:
            raise ApiException(
                400,
                "VALIDATION_ERROR",
                "Combined quantity for one product must be <= 999",
                {"violations": [{"field": "items.quantity", "message": "maximum is 999"}]},
            )
    return merged


def _lock_products_for_order(db, quantities: dict[uuid.UUID, int]) -> dict[uuid.UUID, Product]:
    products: dict[uuid.UUID, Product] = {}
    missing: list[str] = []
    inactive: list[str] = []
    insufficient: list[dict] = []

    for product_id in sorted(quantities, key=str):
        product = db.execute(
            select(Product).where(Product.id == product_id).with_for_update()
        ).scalar_one_or_none()
        if product is None:
            missing.append(str(product_id))
            continue
        products[product_id] = product
        if product.status != "ACTIVE":
            inactive.append(str(product_id))
        elif product.stock < quantities[product_id]:
            insufficient.append(
                {
                    "product_id": str(product_id),
                    "requested": quantities[product_id],
                    "available": product.stock,
                }
            )

    if missing:
        raise ApiException(404, "PRODUCT_NOT_FOUND", "One or more products were not found", {"product_ids": missing})
    if inactive:
        raise ApiException(409, "PRODUCT_INACTIVE", "One or more products are not active", {"product_ids": inactive})
    if insufficient:
        raise ApiException(409, "INSUFFICIENT_STOCK", "Insufficient stock", {"products": insufficient})
    return products


def _promo_for_creation(db, code: str | None, subtotal: Decimal):
    if not code:
        return None, Decimal("0.00")
    promo = db.execute(select(PromoCode).where(PromoCode.code == code).with_for_update()).scalar_one_or_none()
    current = now_utc()
    if (
        promo is None
        or not promo.active
        or promo.current_uses >= promo.max_uses
        or current < promo.valid_from
        or current > promo.valid_until
    ):
        raise ApiException(422, "PROMO_CODE_INVALID", "Promo code is invalid, expired, inactive or exhausted")
    if subtotal < promo.min_order_amount:
        raise ApiException(
            422,
            "PROMO_CODE_MIN_AMOUNT",
            "Order amount is below promo minimum",
            {"min_order_amount": str(promo.min_order_amount), "actual_amount": str(subtotal)},
        )
    return promo, _discount(promo, subtotal)


def _discount(promo: PromoCode, subtotal: Decimal) -> Decimal:
    if promo.discount_type == "PERCENTAGE":
        percentage = min(Decimal(promo.discount_value), Decimal("70"))
        return money(subtotal * percentage / Decimal("100"))
    return money(min(Decimal(promo.discount_value), subtotal))


class AuthApiImpl(BaseAuthApi):
    async def register(self, register_request):
        role = enum_value(register_request.role) if register_request.role is not None else "USER"
        email = str(register_request.email).lower()
        with SessionLocal() as db, db.begin():
            if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
                raise ApiException(409, "USER_ALREADY_EXISTS", "User with this email already exists")
            user = User(email=email, password_hash=hash_password(register_request.password), role=role)
            db.add(user)
            db.flush()
            return {"id": user.id, "email": user.email, "role": user.role, "created_at": user.created_at}

    async def login(self, login_request):
        email = str(login_request.email).lower()
        with SessionLocal() as db, db.begin():
            user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
            if user is None or not verify_password(login_request.password, user.password_hash):
                raise ApiException(401, "TOKEN_INVALID", "Invalid email or password")
            access = create_access_token(user)
            refresh = create_refresh_token(user, db)
            db.flush()
            return {
                "access_token": access,
                "refresh_token": refresh,
                "token_type": "bearer",
                "access_expires_in": settings.access_token_minutes * 60,
            }

    async def refresh(self, refresh_request):
        _, access, refresh = rotate_refresh_token(refresh_request.refresh_token)
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            "access_expires_in": settings.access_token_minutes * 60,
        }


class ProductsApiImpl(BaseProductsApi):
    async def list_products(self, page=0, size=20, status=None, category=None):
        require_role("USER", "SELLER", "ADMIN")
        with SessionLocal() as db:
            filters = []
            if status is not None:
                filters.append(Product.status == enum_value(status))
            if category is not None:
                filters.append(Product.category == category)
            total = db.scalar(select(func.count()).select_from(Product).where(*filters)) or 0
            products = db.execute(
                select(Product)
                .where(*filters)
                .order_by(Product.created_at.desc())
                .offset(page * size)
                .limit(size)
            ).scalars().all()
            return {
                "items": [product_dict(p) for p in products],
                "totalElements": total,
                "page": page,
                "size": size,
            }

    async def create_product(self, product_create):
        principal = require_role("SELLER", "ADMIN")
        requested_seller = getattr(product_create, "seller_id", None)
        seller_id = principal.user_id if principal.role == "SELLER" else requested_seller
        with SessionLocal() as db, db.begin():
            if seller_id is not None and db.get(User, seller_id) is None:
                raise ApiException(400, "VALIDATION_ERROR", "seller_id does not reference an existing user")
            product = Product(
                name=product_create.name,
                description=product_create.description,
                price=money(product_create.price),
                stock=product_create.stock,
                category=product_create.category,
                status=enum_value(product_create.status),
                seller_id=seller_id,
            )
            db.add(product)
            db.flush()
            db.refresh(product)
            return product_dict(product)

    async def get_product(self, id):
        require_role("USER", "SELLER", "ADMIN")
        with SessionLocal() as db:
            product = db.get(Product, id)
            if product is None:
                raise ApiException(404, "PRODUCT_NOT_FOUND", "Product not found")
            return product_dict(product)

    async def update_product(self, id, product_update):
        principal = require_role("SELLER", "ADMIN")
        with SessionLocal() as db, db.begin():
            product = db.execute(select(Product).where(Product.id == id).with_for_update()).scalar_one_or_none()
            if product is None:
                raise ApiException(404, "PRODUCT_NOT_FOUND", "Product not found")
            if principal.role == "SELLER" and product.seller_id != principal.user_id:
                raise ApiException(403, "ACCESS_DENIED", "SELLER can update only own products")
            product.name = product_update.name
            product.description = product_update.description
            product.price = money(product_update.price)
            product.stock = product_update.stock
            product.category = product_update.category
            product.status = enum_value(product_update.status)
            db.flush()
            db.refresh(product)
            return product_dict(product)

    async def delete_product(self, id):
        principal = require_role("SELLER", "ADMIN")
        with SessionLocal() as db, db.begin():
            product = db.execute(select(Product).where(Product.id == id).with_for_update()).scalar_one_or_none()
            if product is None:
                raise ApiException(404, "PRODUCT_NOT_FOUND", "Product not found")
            if principal.role == "SELLER" and product.seller_id != principal.user_id:
                raise ApiException(403, "ACCESS_DENIED", "SELLER can archive only own products")
            product.status = "ARCHIVED"
            db.flush()
        return Response(status_code=204)


class PromoCodesApiImpl(BasePromoCodesApi):
    async def create_promo_code(self, promo_code_create):
        principal = require_role("SELLER", "ADMIN")
        if promo_code_create.valid_until <= promo_code_create.valid_from:
            raise ApiException(
                400,
                "VALIDATION_ERROR",
                "valid_until must be later than valid_from",
                {"violations": [{"field": "valid_until", "message": "must be later than valid_from"}]},
            )
        discount_type = enum_value(promo_code_create.discount_type)
        discount_value = money(promo_code_create.discount_value)
        if discount_type == "PERCENTAGE" and discount_value > Decimal("100"):
            raise ApiException(
                400,
                "VALIDATION_ERROR",
                "Percentage discount cannot exceed 100",
                {"violations": [{"field": "discount_value", "message": "maximum is 100 for PERCENTAGE"}]},
            )
        with SessionLocal() as db, db.begin():
            promo = PromoCode(
                code=promo_code_create.code,
                discount_type=discount_type,
                discount_value=discount_value,
                min_order_amount=money(promo_code_create.min_order_amount),
                max_uses=promo_code_create.max_uses,
                current_uses=0,
                valid_from=promo_code_create.valid_from,
                valid_until=promo_code_create.valid_until,
                active=True if promo_code_create.active is None else promo_code_create.active,
                created_by=principal.user_id,
            )
            db.add(promo)
            try:
                db.flush()
            except IntegrityError as exc:
                raise ApiException(409, "PROMO_CODE_ALREADY_EXISTS", "Promo code already exists") from exc
            return promo_dict(promo)


class OrdersApiImpl(BaseOrdersApi):
    async def create_order(self, order_create):
        principal = require_role("USER", "ADMIN")
        quantities = _normalized_items(order_create.items)
        with SessionLocal() as db, db.begin():
            _lock_user(db, principal.user_id)
            _check_rate_limit(db, principal.user_id, "CREATE_ORDER")
            active = db.execute(
                select(Order.id).where(
                    Order.user_id == principal.user_id,
                    Order.status.in_(["CREATED", "PAYMENT_PENDING"]),
                ).limit(1)
            ).scalar_one_or_none()
            if active is not None:
                raise ApiException(409, "ORDER_HAS_ACTIVE", "User already has an active order")

            products = _lock_products_for_order(db, quantities)

            # Step 5 from the assignment: reserve stock immediately after all
            # catalog/stock checks. If any later step fails, the transaction
            # rolls these changes back automatically.
            price_snapshots: dict[uuid.UUID, Decimal] = {}
            subtotal = Decimal("0.00")
            for product_id, qty in quantities.items():
                product = products[product_id]
                product.stock -= qty
                snapshot = money(product.price)
                price_snapshots[product_id] = snapshot
                subtotal += snapshot * qty
            subtotal = money(subtotal)

            promo, discount = _promo_for_creation(db, order_create.promo_code, subtotal)
            total = money(subtotal - discount)

            order = Order(
                user_id=principal.user_id,
                status="CREATED",
                promo_code_id=promo.id if promo else None,
                total_amount=total,
                discount_amount=discount,
            )
            db.add(order)
            db.flush()

            for product_id, qty in quantities.items():
                db.add(
                    OrderItem(
                        order_id=order.id,
                        product_id=product_id,
                        quantity=qty,
                        price_at_order=price_snapshots[product_id],
                    )
                )
            if promo:
                promo.current_uses += 1
            db.add(UserOperation(user_id=principal.user_id, operation_type="CREATE_ORDER"))
            db.flush()
            db.refresh(order)
            return order_dict(db, order)

    async def get_order(self, id):
        with SessionLocal() as db:
            order = db.get(Order, id)
            if order is None:
                raise ApiException(404, "ORDER_NOT_FOUND", "Order not found")
            _check_order_access(order)
            return order_dict(db, order)

    async def update_order(self, id, order_update):
        quantities = _normalized_items(order_update.items)
        principal = get_current_principal()
        with SessionLocal() as db, db.begin():
            order = db.execute(select(Order).where(Order.id == id).with_for_update()).scalar_one_or_none()
            if order is None:
                raise ApiException(404, "ORDER_NOT_FOUND", "Order not found")
            _check_order_access(order)
            if order.status != "CREATED":
                raise ApiException(409, "INVALID_STATE_TRANSITION", "Order can be updated only in CREATED state")
            _lock_user(db, order.user_id)
            _check_rate_limit(db, order.user_id, "UPDATE_ORDER")

            old_items = db.execute(select(OrderItem).where(OrderItem.order_id == order.id)).scalars().all()
            all_product_ids = sorted({item.product_id for item in old_items} | set(quantities), key=str)
            locked: dict[uuid.UUID, Product] = {}
            for product_id in all_product_ids:
                product = db.execute(
                    select(Product).where(Product.id == product_id).with_for_update()
                ).scalar_one_or_none()
                if product is not None:
                    locked[product_id] = product

            for item in old_items:
                product = locked.get(item.product_id)
                if product is not None:
                    product.stock += item.quantity

            missing = []
            inactive = []
            insufficient = []
            for product_id, qty in quantities.items():
                product = locked.get(product_id)
                if product is None:
                    missing.append(str(product_id))
                elif product.status != "ACTIVE":
                    inactive.append(str(product_id))
                elif product.stock < qty:
                    insufficient.append({"product_id": str(product_id), "requested": qty, "available": product.stock})
            if missing:
                raise ApiException(404, "PRODUCT_NOT_FOUND", "One or more products were not found", {"product_ids": missing})
            if inactive:
                raise ApiException(409, "PRODUCT_INACTIVE", "One or more products are not active", {"product_ids": inactive})
            if insufficient:
                raise ApiException(409, "INSUFFICIENT_STOCK", "Insufficient stock", {"products": insufficient})

            db.execute(delete(OrderItem).where(OrderItem.order_id == order.id))
            subtotal = Decimal("0.00")
            for product_id, qty in quantities.items():
                product = locked[product_id]
                product.stock -= qty
                price_snapshot = money(product.price)
                subtotal += price_snapshot * qty
                db.add(
                    OrderItem(
                        order_id=order.id,
                        product_id=product_id,
                        quantity=qty,
                        price_at_order=price_snapshot,
                    )
                )
            subtotal = money(subtotal)

            discount = Decimal("0.00")
            if order.promo_code_id is not None:
                promo = db.execute(
                    select(PromoCode).where(PromoCode.id == order.promo_code_id).with_for_update()
                ).scalar_one_or_none()
                current = now_utc()
                if promo is None or not promo.active or current < promo.valid_from or current > promo.valid_until or promo.current_uses > promo.max_uses:
                    raise ApiException(422, "PROMO_CODE_INVALID", "Applied promo code is no longer valid")
                if subtotal < promo.min_order_amount:
                    promo.current_uses = max(0, promo.current_uses - 1)
                    order.promo_code_id = None
                else:
                    discount = _discount(promo, subtotal)

            order.discount_amount = money(discount)
            order.total_amount = money(subtotal - discount)
            db.add(UserOperation(user_id=order.user_id, operation_type="UPDATE_ORDER"))
            db.flush()
            db.refresh(order)
            return order_dict(db, order)

    async def cancel_order(self, id):
        with SessionLocal() as db, db.begin():
            order = db.execute(select(Order).where(Order.id == id).with_for_update()).scalar_one_or_none()
            if order is None:
                raise ApiException(404, "ORDER_NOT_FOUND", "Order not found")
            _check_order_access(order)
            if order.status not in {"CREATED", "PAYMENT_PENDING"}:
                raise ApiException(409, "INVALID_STATE_TRANSITION", "Order cannot be canceled from current state")

            items = db.execute(select(OrderItem).where(OrderItem.order_id == order.id)).scalars().all()
            for product_id in sorted({item.product_id for item in items}, key=str):
                db.execute(select(Product).where(Product.id == product_id).with_for_update()).scalar_one()
            for item in items:
                product = db.get(Product, item.product_id)
                product.stock += item.quantity

            if order.promo_code_id is not None:
                promo = db.execute(
                    select(PromoCode).where(PromoCode.id == order.promo_code_id).with_for_update()
                ).scalar_one_or_none()
                if promo is not None:
                    promo.current_uses = max(0, promo.current_uses - 1)
            order.status = "CANCELED"
            db.flush()
            db.refresh(order)
            return order_dict(db, order)

    async def transition_order_status(self, id, order_status_update):
        target = enum_value(order_status_update.status)
        allowed = {
            "CREATED": "PAYMENT_PENDING",
            "PAYMENT_PENDING": "PAID",
            "PAID": "SHIPPED",
            "SHIPPED": "COMPLETED",
        }
        with SessionLocal() as db, db.begin():
            order = db.execute(select(Order).where(Order.id == id).with_for_update()).scalar_one_or_none()
            if order is None:
                raise ApiException(404, "ORDER_NOT_FOUND", "Order not found")
            _check_order_access(order)
            expected = allowed.get(order.status)
            if target != expected:
                raise ApiException(
                    409,
                    "INVALID_STATE_TRANSITION",
                    f"Cannot transition order from {order.status} to {target}",
                    {"current_status": order.status, "requested_status": target, "allowed_status": expected},
                )
            order.status = target
            db.flush()
            db.refresh(order)
            return order_dict(db, order)
