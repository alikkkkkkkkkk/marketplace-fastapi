from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import sys
import time

import httpx

BASE = os.getenv("BASE_URL", "http://localhost:8080")
client = httpx.Client(base_url=BASE, timeout=10.0)


def show(method, path, *, token=None, expected=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = client.request(method, path, headers=headers, **kwargs)
    print(f"\n{method} {path} -> {r.status_code}")
    if r.content:
        try:
            print(r.json())
        except Exception:
            print(r.text)
    if expected is not None and r.status_code != expected:
        print(f"EXPECTED {expected}, GOT {r.status_code}", file=sys.stderr)
        sys.exit(1)
    return r


def login(email, password):
    r = show("POST", "/auth/login", expected=200, json={"email": email, "password": password})
    return r.json()


def main():
    suffix = str(int(time.time()))[-8:]
    seller_email = f"seller{suffix}@example.com"
    user_email = f"user{suffix}@example.com"
    user2_email = f"other{suffix}@example.com"
    password = "Password123!"

    show("POST", "/auth/register", expected=200, json={"email": seller_email, "password": password, "role": "SELLER"})
    show("POST", "/auth/register", expected=200, json={"email": user_email, "password": password, "role": "USER"})
    show("POST", "/auth/register", expected=200, json={"email": user2_email, "password": password, "role": "USER"})

    seller = login(seller_email, password)
    user = login(user_email, password)
    user2 = login(user2_email, password)
    admin = login(os.getenv("ADMIN_EMAIL", "admin@example.com"), os.getenv("ADMIN_PASSWORD", "Admin123!"))

    seller_token = seller["access_token"]
    user_token = user["access_token"]
    user2_token = user2["access_token"]
    admin_token = admin["access_token"]

    # RBAC: USER cannot create a product.
    show(
        "POST",
        "/products",
        token=user_token,
        expected=403,
        json={"name": "Forbidden", "price": 1, "stock": 1, "category": "demo", "status": "ACTIVE"},
    )

    active = show(
        "POST",
        "/products",
        token=seller_token,
        expected=200,
        json={
            "name": "Demo product",
            "description": "Created by E2E script",
            "price": 120.00,
            "stock": 10,
            "category": "demo",
            "status": "ACTIVE",
        },
    ).json()
    product_id = active["id"]

    inactive = show(
        "POST",
        "/products",
        token=seller_token,
        expected=200,
        json={"name": "Inactive product", "price": 50, "stock": 10, "category": "demo", "status": "INACTIVE"},
    ).json()

    promo_code = f"DEMO_{suffix}"
    start = datetime.now(timezone.utc) - timedelta(minutes=1)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    promo = show(
        "POST",
        "/promo-codes",
        token=seller_token,
        expected=200,
        json={
            "code": promo_code,
            "discount_type": "PERCENTAGE",
            "discount_value": 25,
            "min_order_amount": 200,
            "max_uses": 100,
            "valid_from": start.isoformat(),
            "valid_until": end.isoformat(),
            "active": True,
        },
    ).json()

    # Contract validation: quantity=0 must fail before business logic.
    show(
        "POST",
        "/orders",
        token=user_token,
        expected=400,
        json={"items": [{"product_id": product_id, "quantity": 0}]},
    )

    # Catalog invariant.
    show(
        "POST",
        "/orders",
        token=user_token,
        expected=409,
        json={"items": [{"product_id": inactive["id"], "quantity": 1}]},
    )

    # Stock invariant.
    show(
        "POST",
        "/orders",
        token=user_token,
        expected=409,
        json={"items": [{"product_id": product_id, "quantity": 999}]},
    )

    # Promo invariant.
    show(
        "POST",
        "/orders",
        token=user_token,
        expected=422,
        json={"items": [{"product_id": product_id, "quantity": 2}], "promo_code": "NO_SUCH_PROMO"},
    )

    order = show(
        "POST",
        "/orders",
        token=user_token,
        expected=200,
        json={"items": [{"product_id": product_id, "quantity": 2}], "promo_code": promo_code},
    ).json()
    order_id = order["id"]

    # Ownership.
    show("GET", f"/orders/{order_id}", token=user2_token, expected=403)

    # Invalid state transition.
    show(
        "POST",
        f"/orders/{order_id}/status",
        token=user_token,
        expected=409,
        json={"status": "PAID"},
    )

    # Update from 2 units to 1. Subtotal becomes 120 < promo minimum 200,
    # so promo is removed and current_uses is decremented in one transaction.
    show(
        "PUT",
        f"/orders/{order_id}",
        token=user_token,
        expected=200,
        json={"items": [{"product_id": product_id, "quantity": 1}]},
    )

    # Creation rate limit is checked before active-order check.
    show(
        "POST",
        "/orders",
        token=user_token,
        expected=429,
        json={"items": [{"product_id": product_id, "quantity": 1}]},
    )

    # Cancellation returns stock.
    show("POST", f"/orders/{order_id}/cancel", token=user_token, expected=200)

    # Full state machine on a separate ADMIN order.
    admin_order = show(
        "POST",
        "/orders",
        token=admin_token,
        expected=200,
        json={"items": [{"product_id": product_id, "quantity": 1}]},
    ).json()
    admin_order_id = admin_order["id"]
    for target in ["PAYMENT_PENDING", "PAID", "SHIPPED", "COMPLETED"]:
        show(
            "POST",
            f"/orders/{admin_order_id}/status",
            token=admin_token,
            expected=200,
            json={"status": target},
        )
    show("POST", f"/orders/{admin_order_id}/cancel", token=admin_token, expected=409)

    # Refresh-token rotation: old refresh token becomes invalid.
    refreshed = show(
        "POST",
        "/auth/refresh",
        expected=200,
        json={"refresh_token": user["refresh_token"]},
    ).json()
    show(
        "POST",
        "/auth/refresh",
        expected=401,
        json={"refresh_token": user["refresh_token"]},
    )

    # Invalid access token.
    show("GET", "/products", token="not-a-jwt", expected=401)

    # Soft delete: row remains in DB, status becomes ARCHIVED.
    show("DELETE", f"/products/{product_id}", token=seller_token, expected=204)
    archived = show("GET", f"/products/{product_id}", token=admin_token, expected=200).json()
    assert archived["status"] == "ARCHIVED"

    print("\n=== E2E demo completed ===")
    print("Now show DB state:")
    print("docker compose exec -T db psql -U marketplace -d marketplace < scripts/defense_selects.sql")
    print("Or: make db-shell, then paste scripts/defense_selects.sql")
    print("\nTo demonstrate ORDER_HAS_ACTIVE after a successful create when cooldown is enabled:")
    print("1) age CREATE_ORDER in user_operations by > N minutes; 2) repeat POST /orders while current order is CREATED.")
    print("Example SQL: UPDATE user_operations SET created_at = now() - interval '10 minutes' WHERE operation_type='CREATE_ORDER';")


if __name__ == "__main__":
    main()
