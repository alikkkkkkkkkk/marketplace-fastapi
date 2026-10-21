# Шпаргалка на защиту — 20 минут

## 1. Что сказать в начале (20–30 секунд)

> Я сделал contract-first API маркетплейса на Python/FastAPI. Источник истины — OpenAPI YAML. Из него OpenAPI Generator генерирует FastAPI handlers и Pydantic DTO; generated-код не хранится в git. PostgreSQL мигрируется Flyway. Бизнес-логика заказов транзакционная, stock блокируется `FOR UPDATE`. Авторизация — JWT access/refresh, роли USER/SELLER/ADMIN. Каждый API-запрос логируется JSON с request_id.

## 2. Порядок демонстрации

### Шаг 1 — запуск

```bash
docker compose up --build
```

Покажи в логах:

- PostgreSQL healthy;
- Flyway migration success;
- codegen success;
- uvicorn listening on `0.0.0.0:8080`.

### Шаг 2 — Swagger

Открой:

```text
http://localhost:8080/docs
```

Покажи `ProductCreate`, `ProductUpdate`, `ProductResponse`, ErrorResponse, bearerAuth.

### Шаг 3 — E2E

```bash
python scripts/e2e_demo.py
```

Не торопись: проговори 4 ключевых места:

- validation происходит по generated Pydantic model до business method;
- stock резервируется в той же транзакции, что order/order_items;
- `price_at_order` — snapshot;
- cancel возвращает stock и использование промокода.

### Шаг 4 — SELECT в БД

```bash
docker compose exec db psql -U marketplace -d marketplace
```

```sql
SELECT id, name, stock, status, seller_id FROM products;
SELECT id, user_id, status, total_amount, discount_amount FROM orders;
SELECT order_id, product_id, quantity, price_at_order FROM order_items;
SELECT code, current_uses, max_uses FROM promo_codes;
SELECT user_id, operation_type, created_at FROM user_operations ORDER BY created_at DESC;
```

Обязательно ткни в ARCHIVED product: `DELETE` был soft delete.

---

# Теория — ответы, которые нужно знать

## Что такое OpenAPI и зачем contract-first?

OpenAPI — машинно-читаемое описание HTTP API: paths, request/response schemas, validation, security, errors. Contract-first означает, что сначала меняется контракт, затем из него генерируются интерфейсы/DTO, а реализация подстраивается под контракт. Это уменьшает рассинхронизацию документации и кода.

## Что именно у тебя генерируется?

OpenAPI Generator с `python-fastapi` генерирует FastAPI route handlers/base API classes и Pydantic-модели. Моя папка `marketplace_impl` содержит только реализацию бизнес-логики и ORM entities. Ручных request/response DTO нет.

## Почему generated в .gitignore?

Чтобы не хранить производный код. Один и тот же результат воспроизводится из OpenAPI командой `make generate` или при `docker compose up`.

## ORM entity и DTO — это одно и то же?

Нет. ORM entity отражает таблицу БД и нужна persistence-слою. DTO — внешний API-контракт. ORM-классы можно писать вручную; request/response DTO по заданию генерируются из OpenAPI.

## Что такое транзакция?

Набор DB-операций с принципом all-or-nothing. Если создание order прошло, но резерв одного товара не прошёл, исключение откатывает всю транзакцию: не остаётся ни частичного заказа, ни частично уменьшенного stock.

## Зачем `SELECT ... FOR UPDATE`?

Это pessimistic row lock. Пока одна транзакция меняет stock товара, конкурентная транзакция ждёт. Поэтому две покупки не могут одновременно прочитать один и тот же старый stock и обе успешно его списать.

## Почему товары блокируются в одинаковом порядке?

Чтобы уменьшить риск deadlock. Если несколько транзакций блокируют несколько товаров, они сортируют product IDs и берут lock в одном порядке.

## Почему одной проверки `product.stock >= quantity` недостаточно?

Без lock две транзакции могут одновременно увидеть stock=1 и обе списать по 1. Проверка должна быть внутри транзакции на заблокированной строке либо делаться атомарным UPDATE с условием.

## Зачем `price_at_order`?

Это snapshot цены на момент заказа. Если продавец завтра изменит `products.price`, старый заказ не должен поменять сумму задним числом.

## Что такое soft delete?

Запись физически остаётся в таблице. `DELETE /products/{id}` меняет `status` на `ARCHIVED`. Это сохраняет историю и ссылки из старых заказов.

## Почему есть DB partial unique index на активный заказ, если проверка уже есть в коде?

Кодовая проверка удобна для понятной ошибки, а DB constraint защищает инвариант от race condition и ошибок в других путях записи. Защита на двух уровнях.

## Почему rate limit хранится в `user_operations`, а не только в памяти?

Потому что приложение может иметь несколько экземпляров и перезапускаться. База — общий источник состояния. In-memory limiter потерялся бы при рестарте и не синхронизировался бы между инстансами.

## Разница 400, 401, 403, 404, 409, 422, 429

- 400 — структура/validation запроса неверна;
- 401 — нет валидной аутентификации;
- 403 — пользователь известен, но прав недостаточно;
- 404 — ресурс не найден;
- 409 — конфликт с текущим бизнес-состоянием;
- 422 — запрос синтаксически валиден, но promo business rule не выполнен;
- 429 — слишком частые операции.

## Access token и refresh token

Access короткий — используется на каждом API-запросе. Refresh длинный — нужен только чтобы выпустить новый access. У меня refresh имеет `jti`, хранится в БД и ротируется; повторное использование старого refresh запрещено.

## Почему role в JWT, а ownership всё равно проверяется по БД?

Role отвечает за класс разрешений, но `SELLER` не должен управлять товаром другого seller, а USER — заказом другого USER. Для этого сравнивается `seller_id`/`order.user_id` с `sub` из JWT.

## Зачем Flyway, если SQL можно выполнить руками?

Flyway версионирует схему и гарантирует воспроизводимый порядок миграций. Ручное выполнение SQL не даёт надёжно понять, какая версия схемы установлена на конкретной БД.

## Что делает trigger `updated_at`?

PostgreSQL сам меняет `updated_at = now()` перед UPDATE. Значение не зависит от того, не забыл ли разработчик обновить поле в конкретном endpoint.

## Что такое индекс и зачем индекс по status?

Индекс — дополнительная структура данных, ускоряющая поиск ценой дискового места и небольшой стоимости записи. `products.status` часто участвует в фильтрации, и индекс по нему отдельно требуется заданием.

## Почему JSON logs?

Их легко собирать и искать в ELK/Loki/Datadog. Поля имеют стабильную структуру. `request_id` связывает один запрос с логами и возвращается клиенту через `X-Request-Id`.

---

# Вопросы-ловушки

## “Что произойдёт, если после возврата старого stock при update новый товар невалиден?”

Будет exception, и вся транзакция rollback. Возврат старого stock тоже откатится — исходный заказ и stock останутся как были.

## “Два человека одновременно покупают последний товар. Что происходит?”

Первый получает row lock. Второй ждёт. Первый уменьшает stock и commit. Второй после получения lock видит новый stock и получает `INSUFFICIENT_STOCK`.

## “Почему второй POST /orders сразу даёт rate limit, а не ORDER_HAS_ACTIVE?”

Потому что в задании проверки перечислены последовательно: rate limit идёт первым, active-order check вторым. Реализация сохраняет именно этот порядок.

## “Что если промокод был применён, а после update сумма ниже min_order_amount?”

В той же транзакции скидка снимается, `promo_code_id` очищается и `current_uses` уменьшается на 1.

## “Можно ли CANCEL из PAID?”

Нет. По контракту cancel разрешён только из CREATED и PAYMENT_PENDING. Получим `INVALID_STATE_TRANSITION`.

## “Почему SELLER не может POST /orders?”

Так задана role matrix. Это не техническое ограничение FastAPI, а бизнес-policy.

## “Может ли SELLER передать чужой seller_id при создании товара?”

Нет. Для SELLER `seller_id` принудительно берётся из `sub` JWT. Поле из body учитывается только для ADMIN.

---

# Если что-то попросили изменить прямо на защите

1. Сначала найди правило в `openapi.yaml`, если это контракт/validation.
2. Для business rule — `marketplace_impl/api_impl.py`.
3. Для schema/index — новый Flyway migration, а не ручное изменение production DB.
4. После изменения OpenAPI перегенерируй: `make generate`.
