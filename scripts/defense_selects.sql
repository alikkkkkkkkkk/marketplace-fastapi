\x on
SELECT id, email, role, created_at FROM users ORDER BY created_at DESC;
SELECT id, name, price, stock, status, seller_id, created_at, updated_at FROM products ORDER BY created_at DESC;
SELECT id, user_id, status, promo_code_id, total_amount, discount_amount, created_at, updated_at FROM orders ORDER BY created_at DESC;
SELECT id, order_id, product_id, quantity, price_at_order FROM order_items ORDER BY id;
SELECT id, code, discount_type, discount_value, min_order_amount, max_uses, current_uses, active, valid_from, valid_until FROM promo_codes ORDER BY code;
SELECT id, user_id, operation_type, created_at FROM user_operations ORDER BY created_at DESC;
