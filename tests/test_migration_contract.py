from pathlib import Path

SQL = (Path(__file__).parents[1] / "db/migration/V1__init.sql").read_text()


def test_required_tables_exist():
    for table in ["products", "orders", "order_items", "promo_codes", "user_operations"]:
        assert f"CREATE TABLE {table}" in SQL


def test_status_index_exists():
    assert "CREATE INDEX idx_products_status ON products(status)" in SQL


def test_active_order_race_is_protected_in_database():
    assert "CREATE UNIQUE INDEX uq_orders_one_active_per_user" in SQL
    assert "WHERE status IN ('CREATED','PAYMENT_PENDING')" in SQL


def test_updated_at_is_automatic():
    assert "CREATE TRIGGER trg_products_updated_at" in SQL
    assert "CREATE TRIGGER trg_orders_updated_at" in SQL
