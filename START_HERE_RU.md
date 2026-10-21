# НАЧНИ ОТСЮДА — защита на 10/10

Тебе не нужно знать Java. Проект написан на **Python + FastAPI**. Для запуска нужен только Docker Desktop (или Docker Engine + Compose).

## 1. Перед первым запуском

В папке проекта:

```bash
cp .env.example .env
```

Открой `.env` и замени `JWT_SECRET` на длинную случайную строку.

## 2. Поднять проект

```bash
docker compose up --build
```

Дождись, чтобы контейнер `app` написал, что Uvicorn слушает `0.0.0.0:8080`.

Открой:

- Swagger: http://localhost:8080/docs
- API: http://localhost:8080

## 3. Самая простая демонстрация

Во втором терминале:

```bash
python -m pip install httpx
python scripts/e2e_demo.py
```

Скрипт специально прогоняет не только happy path, но и ошибки: validation, RBAC, inactive product, insufficient stock, promo, ownership, rate limit, state transition, refresh token и soft delete.

## 4. Обязательно показать БД

После E2E:

```bash
docker compose exec -T db psql -U marketplace -d marketplace < scripts/defense_selects.sql
```

На защите словами подчеркни:

- `DELETE /products/{id}` не удаляет строку: ставит `ARCHIVED`;
- `price_at_order` — снимок цены, поэтому изменение цены товара не меняет старый заказ;
- stock резервируется внутри одной транзакции;
- `SELECT ... FOR UPDATE` защищает остатки от конкурентных заказов;
- partial unique index в PostgreSQL не разрешает два активных заказа одному пользователю;
- DTO и роутеры генерируются из OpenAPI, вручную написаны только ORM и бизнес-логика.

## 5. Что читать перед защитой

1. `DEFENSE_CHEATSHEET.md` — готовые ответы на теорию и порядок 20-минутной защиты.
2. `RUBRIC_CHECKLIST.md` — где в проекте закрыт каждый из 10 баллов.
3. `src/main/resources/openapi/openapi.yaml` — контракт.
4. `marketplace_impl/api_impl.py` — основная бизнес-логика.
5. `db/migration/V1__init.sql` — схема БД, индексы и triggers.

## 6. Если преподаватель спрашивает «почему такой стек?»

Короткий ответ:

> Я выбрал contract-first подход: OpenAPI — источник истины, из него автоматически генерируются FastAPI handlers и DTO. Бизнес-логика отделена от сгенерированного слоя. PostgreSQL отвечает за целостность данных, Flyway — за воспроизводимые миграции, а транзакции и row locks — за корректность резервирования остатков при конкурентных запросах.

Не заучивай код построчно. Понимай 5 вещей: **контракт → generated layer → бизнес-логика → транзакция/БД → JWT/RBAC**.
