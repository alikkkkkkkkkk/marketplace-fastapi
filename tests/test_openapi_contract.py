from pathlib import Path
import yaml

SPEC = Path(__file__).parents[1] / "src/main/resources/openapi/openapi.yaml"


def load_spec():
    return yaml.safe_load(SPEC.read_text())


def test_required_product_crud_exists():
    spec = load_spec()
    paths = spec["paths"]
    assert "post" in paths["/products"]
    assert "get" in paths["/products"]
    assert {"get", "put", "delete"}.issubset(paths["/products/{id}"])


def test_product_constraints_are_in_contract():
    schemas = load_spec()["components"]["schemas"]
    product = schemas["ProductCreate"]["properties"]
    assert product["name"]["minLength"] == 1
    assert product["name"]["maxLength"] == 255
    assert product["description"]["maxLength"] == 4000
    assert product["price"]["minimum"] == 0.01
    assert product["stock"]["minimum"] == 0
    assert product["category"]["maxLength"] == 100


def test_order_constraints_are_in_contract():
    schemas = load_spec()["components"]["schemas"]
    items = schemas["OrderCreate"]["properties"]["items"]
    assert items["minItems"] == 1
    assert items["maxItems"] == 50
    quantity = schemas["OrderItemInput"]["properties"]["quantity"]
    assert quantity["minimum"] == 1
    assert quantity["maximum"] == 999
    assert schemas["OrderCreate"]["properties"]["promo_code"]["pattern"] == "^[A-Z0-9_]{4,20}$"


def test_required_error_codes_are_declared():
    required = {
        "PRODUCT_NOT_FOUND",
        "PRODUCT_INACTIVE",
        "ORDER_NOT_FOUND",
        "ORDER_LIMIT_EXCEEDED",
        "ORDER_HAS_ACTIVE",
        "INVALID_STATE_TRANSITION",
        "INSUFFICIENT_STOCK",
        "PROMO_CODE_INVALID",
        "PROMO_CODE_MIN_AMOUNT",
        "ORDER_OWNERSHIP_VIOLATION",
        "VALIDATION_ERROR",
    }
    codes = set(load_spec()["components"]["schemas"]["ErrorCode"]["enum"])
    assert required <= codes


def test_every_non_auth_operation_is_secured():
    spec = load_spec()
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "delete", "patch"}:
                continue
            if path.startswith("/auth/"):
                continue
            assert operation.get("security") == [{"bearerAuth": []}], (method, path)
