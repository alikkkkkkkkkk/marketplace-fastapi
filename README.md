# Marketplace API — Python/FastAPI, contract-first

Полное решение домашнего задания Marketplace: OpenAPI + CRUD на Python.

## Стек

- Python 3.12 + FastAPI
- PostgreSQL 16
- SQLAlchemy 2
- Flyway для миграций
- OpenAPI Generator (`python-fastapi`) для серверных stubs и DTO/Pydantic-моделей
- JWT access + refresh tokens
- Docker Compose
- pytest

Главный принцип: **OpenAPI — источник истины**. DTO и FastAPI-роутеры не пишутся руками. Они генерируются из `src/main/resources/openapi/openapi.yaml` и лежат в `generated/`, который добавлен в `.gitignore`.

## Структура

```text
.
├── src/main/resources/openapi/openapi.yaml   # контракт API
├── templates/python-fastapi/                 # 3 небольших override-шаблона генератора
├── marketplace_impl/                         # бизнес-логика, DB, JWT, RBAC, logging
├── db/migration/V1__init.sql                 # Flyway migration
├── scripts/e2e_demo.py                       # готовая демонстрация для защиты
├── scripts/defense_selects.sql               # SELECT'ы для показа таблиц
├── tests/                                    # проверки контракта/миграции
├── Dockerfile
├── docker-compose.yml
└── Makefile
```

## Быстрый запуск

Нужен только Docker Desktop / Docker Engine с Compose.

```bash
cp .env.example .env
# обязательно поменяй JWT_SECRET в .env перед сдачей

docker compose up --build
```

Compose делает три вещи до старта приложения:

1. поднимает PostgreSQL;
2. Flyway применяет `db/migration/V1__init.sql`;
3. OpenAPI Generator генерирует серверные роутеры и модели во временный Docker volume.

API: `http://localhost:8080`

Swagger UI: `http://localhost:8080/docs`

Администратор создаётся автоматически при первом старте:

```text
email:    admin@example.com
password: Admin123!
```

Пароль можно поменять через `ADMIN_EMAIL` / `ADMIN_PASSWORD` в `.env`.

## Отдельная кодогенерация

```bash
make generate
```

Эквивалентная команда без Make:

```bash
docker run --rm \
  -v "$PWD:/local" \
  openapitools/openapi-generator-cli:v7.26.0 generate \
  -i /local/src/main/resources/openapi/openapi.yaml \
  -g python-fastapi \
  -o /local/generated \
  -t /local/templates/python-fastapi \
  --additional-properties=packageName=marketplace_api,fastapiImplementationPackage=marketplace_impl,useExternalImplementationPackage=true,sourceFolder=src
```

`generated/` намеренно не коммитится.

## Тесты

После запуска Docker или отдельно:

```bash
make test
```

Тесты проверяют обязательные ограничения OpenAPI, security на endpoint'ах, error codes, наличие таблиц, индекса `products.status`, автоматического `updated_at` и DB-защиты от двух активных заказов.

## Готовая E2E-демонстрация

В другом терминале:

```bash
python -m pip install httpx
python scripts/e2e_demo.py
```

Скрипт показывает:

- регистрацию и login;
- RBAC (`USER` не может создавать товар);
- создание ACTIVE и INACTIVE товара;
- создание промокода;
- `VALIDATION_ERROR` на `quantity=0`;
- `PRODUCT_INACTIVE`;
- `INSUFFICIENT_STOCK`;
- `PROMO_CODE_INVALID`;
- создание заказа с резервом stock и snapshot цены;
- ownership check;
- `INVALID_STATE_TRANSITION`;
- обновление заказа и снятие промокода при падении суммы ниже `min_order_amount`;
- rate limit;
- cancel с возвратом stock;
- полную цепочку статусов `CREATED → PAYMENT_PENDING → PAID → SHIPPED → COMPLETED`;
- refresh-token rotation;
- `TOKEN_INVALID`;
- soft delete товара: строка остаётся, статус становится `ARCHIVED`.

## Что показать в PostgreSQL

После E2E:

```bash
docker compose exec db psql -U marketplace -d marketplace
```

Дальше можно вставить содержимое `scripts/defense_selects.sql`.

Минимальный набор:

```sql
SELECT id, name, price, stock, status, seller_id FROM products;
SELECT id, user_id, status, promo_code_id, total_amount, discount_amount FROM orders;
SELECT order_id, product_id, quantity, price_at_order FROM order_items;
SELECT code, current_uses, max_uses FROM promo_codes;
SELECT user_id, operation_type, created_at FROM user_operations ORDER BY created_at DESC;
```

Особенно важно показать, что после `DELETE /products/{id}` запись **не исчезла**, а имеет `status = 'ARCHIVED'`.

## Архитектура бизнес-логики заказов

### Создание заказа

В одной DB-транзакции:

1. блокируется строка пользователя (`FOR UPDATE`) — это сериализует конкурентные создания заказа одного пользователя;
2. проверяется rate limit;
3. проверяется отсутствие активного заказа;
4. товары блокируются `SELECT ... FOR UPDATE` в стабильном порядке ID;
5. проверяются `ACTIVE` и stock;
6. цена копируется в `order_items.price_at_order`;
7. stock уменьшается;
8. проверяется/блокируется promo code;
9. считается скидка и сумма;
10. создаётся `user_operations(CREATE_ORDER)`.

Если на любом шаге ошибка — транзакция откатывается целиком.

Дополнительно в PostgreSQL есть partial unique index:

```sql
CREATE UNIQUE INDEX uq_orders_one_active_per_user
ON orders(user_id)
WHERE status IN ('CREATED','PAYMENT_PENDING');
```

То есть инвариант защищён не только кодом, но и базой.

### Обновление заказа

Разрешено только в `CREATED`.

В одной транзакции:

- возвращается stock старых позиций;
- блокируются товары;
- проверяются новые позиции;
- резервируется новый stock;
- старые `order_items` заменяются;
- новые цены снова фиксируются snapshot'ом;
- сумма пересчитывается;
- если применённый промокод больше не проходит `min_order_amount`, скидка снимается, `promo_code_id = NULL`, `current_uses -= 1`;
- пишется `UPDATE_ORDER` в `user_operations`.

### Отмена

Только из `CREATED` и `PAYMENT_PENDING`:

- возвращается весь stock;
- `promo_code.current_uses` уменьшается;
- статус становится `CANCELED`;
- всё в одной транзакции.

## JWT и роли

Access token живёт 20 минут по умолчанию, refresh token — 14 дней.

JWT содержит:

```json
{
  "sub": "user-uuid",
  "role": "USER|SELLER|ADMIN",
  "type": "access|refresh"
}
```

Refresh token сохраняется по `jti` в БД и ротируется: после `/auth/refresh` старый refresh token помечается `revoked=true` и повторно использовать его нельзя.

Матрица доступа реализована в `marketplace_impl/api_impl.py`:

- USER: читает товары, работает только со своими заказами;
- SELLER: создаёт/меняет/архивирует только свои товары, создаёт промокоды, не работает с заказами;
- ADMIN: может работать с любыми товарами/заказами.

## API logging

Каждый запрос логируется одной JSON-строкой:

```json
{
  "request_id": "...",
  "method": "POST",
  "endpoint": "/orders",
  "status_code": 200,
  "duration_ms": 13.42,
  "user_id": "...",
  "timestamp": "2026-10-21T...+00:00",
  "request_body": {"items": "..."}
}
```

Для `POST` / `PUT` / `DELETE` сохраняется request body. Поля `password`, `refresh_token`, `access_token`, `token` маскируются. `X-Request-Id` возвращается в response header.

## Как показать ORDER_HAS_ACTIVE при включённом rate limit

По условию rate limit проверяется **раньше** активного заказа, поэтому сразу после успешного создания второй `POST /orders` закономерно даст `ORDER_LIMIT_EXCEEDED`.

Чтобы отдельно показать `ORDER_HAS_ACTIVE`, на защите можно состарить запись операции:

```sql
UPDATE user_operations
SET created_at = now() - interval '10 minutes'
WHERE operation_type = 'CREATE_ORDER';
```

После этого повторить `POST /orders` для того же пользователя, пока его первый заказ всё ещё `CREATED`.

