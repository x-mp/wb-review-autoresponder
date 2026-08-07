# WB Review Assistant

Отдельный FastAPI-сервис для безопасной работы с отзывами и вопросами Wildberries: сбор данных и карточек, AI-черновики через OpenRouter, ручное согласование, контролируемая публикация, статистика и подготовка обоснованных жалоб.

## Что уже реализовано

- официальные WB API для вопросов, активных/архивных отзывов и полного каталога продавца;
- append-only raw-архив каждого ответа API с временем, URL, параметрами, HTTP-статусом, content type, SHA-256 и ссылкой на документацию;
- PostgreSQL с товарами, обращениями, черновиками, жалобами, аудитом изменений и журналом каждого доступа;
- генерация через OpenRouter со structured output, редактированием персональных данных и локальной policy-проверкой;
- ручное одобрение, глобальный `AUTO_PUBLISH=false`, read-before и read-back при записи;
- FastAPI-панель и REST API, защищённые Bearer-токеном;
- Docker Compose: отдельные `app` и `worker`, непривилегированный пользователь, read-only filesystem;
- анализ оснований жалобы. Отправка жалобы остаётся ручной: официального seller API для неё нет, cookie-автоматизация кабинета не используется.

## Быстрый старт

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
# вставьте результат в APP_ACCESS_TOKEN
docker compose run --rm app wb-reviews seed-demo
docker compose up --build
```

Откройте `http://localhost:8000/login` и введите `APP_ACCESS_TOKEN`. Для реальных данных задайте `WB_API_TOKEN`, затем запустите `docker compose up --build`. Worker синхронизирует данные с заданным интервалом.

OpenRouter включается через `LLM_MODE=openrouter`, `OPENROUTER_API_KEY` и `OPENROUTER_MODEL`. Реальные ответы не отправляются, пока оператор не одобрит черновик и администратор явно не установит `AUTO_PUBLISH=true`.

## Команды

```bash
wb-reviews init-db
wb-reviews seed-demo
wb-reviews sync
wb-reviews worker
pytest -q
ruff check .
```

Подробная спецификация: [docs/SPEC.md](docs/SPEC.md). Актуальные правила жалоб: [docs/WB_RULES.md](docs/WB_RULES.md).

## Секреты и данные

Не коммитьте `.env`, `secrets/`, дампы PostgreSQL и `data/raw`. WB-токен передаётся непосредственно в заголовке `Authorization` и никогда не пишется в manifest или PostgreSQL. Для production рекомендуется Docker secret или внешний secret manager и TLS reverse proxy.

## PostgreSQL и аудит

PostgreSQL не публикуется наружу и хранится в volume `wb_review_postgres`. Таблица `audit_log` фиксирует изменения состояния с actor, entity, request ID, временем и безопасными деталями. `access_log` фиксирует каждый запрос к UI/API: успешную или неуспешную авторизацию, метод, путь, только имена query-параметров, HTTP-статус, IP, user-agent и длительность. Значения токенов, Authorization, cookie и тела форм входа не сохраняются.

Для просмотра журналов используйте защищённые endpoints `GET /api/audit` и `GET /api/access-log`. Резервная копия:

```bash
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > backup.dump
```
