# PriceCom — Project Architecture

Date: 2026-03-10

This document describes the current architecture of the PriceCom project (backend, frontend, APIs, database schema, and major call flows). It was produced by static analysis of the repository source code and templates. It does NOT modify code.

---

**1) Project Overview**

- Purpose: PriceCom is a price intelligence platform that scrapes e-commerce listings, stores product and price data, runs price-history analytics, supports watchlists/alerts, and provides a dashboard for searching and AI-assisted OCR-based product lookups.
- Main features:
  - Universal product search and scraping (SerpAPI-backed ScraperService)
  - Price ingestion & history (StorePrice, PriceHistory)
  - Watchlist and price alerts (Watchlist, PriceAlert)
  - OCR image-to-query pipeline (Tesseract via `ProductImage` + worker tasks)
  - HTMX-driven dashboard UI with partial updates
  - Background tasks via Celery / django-q (multiple @shared_task definitions)
  - Auth and user/wallet management (custom `User`, `Wallet` models)


**2) Backend Architecture**

Framework: Django (project root `config/`), apps: `apps.dashboard`, `apps.scraper`, `apps.accounts`, `core`, `authentication`.
Workers: Celery tasks in `apps.scraper.tasks` and `apps.dashboard.tasks`.

API Endpoint Map (discovered from `config/urls.py`, `apps/*/urls.py`, templates):

Endpoint | Method | Called From | Backend Function | Models Used | Response Type
---|---:|---|---|---|---
/dashboard/api/products/ | GET | Dashboard templates (`price_table`) via HTMX | `apps.dashboard.views.api_products` | `Product`, `StorePrice` | HTML partial (`dashboard/partials/product_rows.html`)
/dashboard/api/products/<int:uuid>/history/ | GET | Product row click / chart requests | `apps.dashboard.views.api_product_history` | `Product`, `PriceHistory` | JSON chart payload
/dashboard/api/watchlist/ | GET | Dashboard watchlist panels (`watchlist_panel.html`) | `apps.dashboard.views.api_watchlist` | `Watchlist`, `Product`, `StorePrice` | HTML partial
/dashboard/api/system-health/ | GET | Dashboard system health widgets | `apps.dashboard.views.api_system_health` | `StorePrice`, `PriceHistory` (stats) | HTML partial
/dashboard/api/search/ | POST | Search bar (`search_panel.html`, `ai_shopper_panel.html`) via HTMX | `apps.dashboard.views.api_search` → `ScraperService.scrape()`, `persist_results()` | `Product`, `StorePrice`, `PriceHistory` (persist) | HTML partial or full render
/dashboard/api/image-search/ | POST | Camera upload (search_panel, image_upload_card) — JS file upload or HTMX | `apps.dashboard.views.api_image_search` → queues `image_search_task` (Celery) or fallback thread; uses `pytesseract` locally | `ProductImage` (saved), then `Product`, `StorePrice` via search pipeline | JSON { task_id, ocr_text }
/dashboard/api/result/<task_id>/ | GET | Image-upload polling UI (`image_loading.html`) | `apps.dashboard.views.api_result` | Internal in-memory `_IMAGE_TASKS` or Redis cache | JSON { status, results, chart }
/scraper/search/ | GET/POST | Scraper UI (`scraper/dashboard.html`) | `apps.scraper.views.ProductSearchView` → `get_coordinated_data()` service | `Product`, `StorePrice` | HTML page / partial
/scraper/task_status/<task_id>/ | GET | Task status polls (task runner) | `apps.scraper.views.TaskStatusView` | `django_q.models.Task` | JSON {status, result}
/scraper/watchlist/ | GET | Scraper watchlist page | `apps.scraper.views.WatchlistView` | `Watchlist`, `Product`, `StorePrice` | HTML page/partial
/scraper/watchlist/toggle/ | POST | Toggle button in product rows / templates (HTMX) | `apps.scraper.views.ToggleWatchlistView` | `Watchlist`, `Product` | HTML fragment (button)
/scraper/api/history/<product_id>/ | GET | Price chart partial (`price_chart.html`) | `apps.scraper.views.PriceHistoryAPIView` | `PriceHistory`, `StorePrice`, `Product` | JSON list of historical points
/accounts/* (register/login/logout/password flows) | GET/POST | Login/Register pages and allauth flows | `authentication.urls` + `apps.accounts.views` + allauth | `User`, `Wallet`, `WalletTransaction` | HTML / Redirects / JSON (where used)
/admin/ | GET/POST | Admin UI | `django.contrib.admin` | All models | HTML admin
/
(home) | GET | Static home page | `TemplateView('home.html')` | none | HTML

Notes:
- The system relies heavily on HTMX for incremental updates (templates contain `hx-get`/`hx-post` attributes).
- `apps.dashboard.views.api_search` is the main glue that calls `core.services.ScraperService` (search + scrape + persist).
- OCR is handled synchronously in `api_image_search` (extract text then enqueue search task) and with a dedicated worker task `process_product_image_ocr` in `apps.scraper.tasks` (the worker also performs OCR and triggers `search_and_scrape_task`).


**3) Frontend Architecture**

The frontend is server-rendered Django templates, enhanced with HTMX and Alpine.js for small interactions. Key templates live under `apps/dashboard/templates/` and `apps/scraper/templates/`.

Pages / Components (high-level):

- Dashboard Home (`apps/dashboard/templates/dashboard/index.html`)
  - `SearchPanel` (file: `cotton/search_panel.html`)
    - Input search box — HTMX `hx-post` to `/dashboard/api/search/` (submits search text)
    - Camera upload — triggers `fetch('/dashboard/api/image-search/', ...)` (JS) which returns `ocr_text` and `task_id`; search input populated and form auto-submitted.
  - `PriceTable` (`partials/price_table.html`) — HTMX `hx-get` `/dashboard/api/products/` to load rows
  - `ProductRows` (`partials/product_rows.html`) — click triggers `hx-get` `/dashboard/api/products/<id>/history/` to load chart/history
  - `ImageUploadCard` — can use `hx-post` `/dashboard/api/image-search/` in some partials
  - `WatchlistPanel` (`partials/watchlist_panel.html`) — hx-get `/dashboard/api/watchlist/`
  - `SystemHealth` — hx-get `/dashboard/api/system-health/`, polls every 60s
  - `AI Shopper Panel` (`partials/ai_shopper_panel.html`) — hx-post `/dashboard/api/search/`
  - `Image Loading` partial (`partials/image_loading.html`) — polls `/dashboard/api/result/<task_id>/` until status `SUCCESS` and then renders results

- Scraper UI (`apps/scraper/templates/scraper/dashboard.html`)
  - `Search` → calls `/scraper/search/` (GET or POST depending on integration) — returns search partials
  - `Watchlist` page → GET `/scraper/watchlist/`
  - `ToggleWatchlist` button → POST `/scraper/watchlist/toggle/` (HTMX)
  - `PriceChart` partial fetch → `/scraper/api/history/<product_id>/`

User interactions that trigger calls:
- Typing a query + submit → `/dashboard/api/search/` (HTMX POST)
- Clicking product row → fetch product history (`/dashboard/api/products/<id>/history/`)
- Clicking camera + selecting file → POST `/dashboard/api/image-search/` via JS -> auto submit search
- Polling image task status → GET `/dashboard/api/result/<task_id>/`
- Page loads where HTMX targets exist will fetch product rows/watchlist/system-health automatically on load.


**4) Database Schema (models summary)**

Primary models (from `apps/scraper.models` and `apps.accounts.models`):

- Category
  - name, slug, icon
  - Relationship: Product.category -> FK to Category

- Tag
  - name, slug
  - Relationship: Product.tags -> M2M Tag

- Product
  - uuid, name, slug, sku, brand_name
  - FK category, M2M tags
  - base_price, current_lowest_price, is_active, is_featured
  - search_vector, metadata(JSON), trend_indicator
  - created_at, updated_at
  - Relationships: Product.prices -> StorePrice, Product.watchlist_items -> Watchlist, Product.notification_logs -> NotificationLog, Product.images -> ProductImage

- StorePrice
  - product (FK), store_name, current_price, product_url, image_url, is_available, metadata(JSON), last_updated, price_hash
  - unique_together: (product, store_name)
  - Relationships: StorePrice.history -> PriceHistory

- PriceHistory
  - store_price (FK), price, currency, change_percentage, trend, is_significant_drop, integrity_hash, metadata(JSON), recorded_at

- Watchlist
  - uuid, user (FK to User), product (FK), target_price, reward flags, last_notified_price, created_at
  - unique_together: (user, product)

- PriceAlert
  - uuid, user (FK), product_url, target_price, current_price, alert_priority, is_triggered, created_at

- NotificationLog
  - uuid, user (FK), product (FK nullable), price_at_alert, status, alert_type, intent_timestamp, smtp_response_code, error_message

- ProductImage
  - uuid, user (FK), image (ImageField -> `ocr_uploads/%Y/%m/%d/`), status, extracted_text, processed_at, created_at

- User (custom `apps.accounts.models.User`)
  - email (USERNAME_FIELD), profile_picture, phone_number, is_verified, is_premium, alert_frequency, profile_updated_at
  - Wallet (OneToOne), WalletTransaction (logs)

- Wallet & WalletTransaction (payment/rewards ledger)
  - Wallet.user FK, balance
  - WalletTransaction: tx_uuid, wallet FK, tx_type, amount, running_balance, idempotency_key, security_hash

Relationships summary:
- Product 1:N StorePrice 1:N PriceHistory
- Product N:M Tag
- Category 1:N Product
- User 1:N Watchlist; Watchlist -> Product (N side)
- User 1:N PriceAlert, NotificationLog
- User 1:N ProductImage
- User 1:1 Wallet -> WalletTransaction (1:N)


**5) Full Feature Workflows**

A. OCR Product Search (image → product search)
- User selects file in dashboard search panel
- Frontend JS POSTs file to `/dashboard/api/image-search/` (AJAX) or HTMX `hx-post`
- `apps.dashboard.views.api_image_search` saves image as `ProductImage`, runs `pytesseract.image_to_string` synchronously to extract raw text (or falls back if libs missing), normalizes text via `core.services.query_cleaner.normalize_query`, returns `ocr_text` and `task_id`.
- Frontend receives `ocr_text` and populates search input, then triggers normal search flow (HTMX submit to `/dashboard/api/search/`). `api_image_search` also enqueues `image_search_task` (Celery) that will run search+persist asynchronously.
- `apps.scraper.tasks.process_product_image_ocr` is the worker OCR path for images created/queued via other flows — it reads the image, pre-processes, sets `pytesseract.pytesseract.tesseract_cmd` from `settings.TESSERACT_CMD` (fallback), extracts text, saves `ProductImage.extracted_text`, and triggers `search_and_scrape_task` with the cleaned query.
- Search service scrapes marketplaces, persists `Product`, `StorePrice`, `PriceHistory`. Frontend polls `/dashboard/api/result/<task_id>/` for final results and renders them.

B. Product Search (text)
- User types query in search box, HTMX posts to `/dashboard/api/search/`.
- `apps.dashboard.views.api_search` calls `normalize_query()` then `ScraperService.scrape(clean_q)`.
- If results returned: `ScraperService.persist_results` writes/updates `Product`, `StorePrice`, and `PriceHistory`. A chart payload created from DB and partial HTML (rows) returned.
- If no results: the handler returns an informative error partial.

C. Price History
- Product row click or chart requests call `/dashboard/api/products/<id>/history/` or `/scraper/api/history/<id>/`.
- `apps.dashboard.views.api_product_history` / `apps.scraper.views.PriceHistoryAPIView` query `PriceHistory` filtered to recent days and return JSON for chart rendering.

D. Watchlist & Alerts
- Toggle via `/scraper/watchlist/toggle/` POST — `ToggleWatchlistView` creates/deletes `Watchlist` entries and returns a fresh button fragment.
- `/dashboard/api/watchlist/` loads a user's watchlist (HTMX) from `Watchlist` model.
- Price alerts exist in `PriceAlert` model; separate background job evaluates triggers and uses `NotificationLog` to record sends.

E. Background Workers (Celery / django-q)
- Tasks in `apps.scraper.tasks` implement scraping, OCR processing, sync refreshes, and authenticity checks. They read/write `StorePrice`, `PriceHistory`, and update `Product` state.


**6) Code Call Flow (file-level examples)**

- Frontend `cotton/search_panel.html` (X) -> POST `/dashboard/api/search/` -> `apps.dashboard.views.api_search` -> `core.services.ScraperService.scrape()` -> `apps.scraper.services.services.persist_results()` -> `Product`, `StorePrice`, `PriceHistory` updates.

- Frontend image upload (JS fetch) -> POST `/dashboard/api/image-search/` -> `apps.dashboard.views.api_image_search` -> local `pytesseract` extraction -> `normalize_query()` -> enqueue `image_search_task` -> `apps.scraper.tasks.process_product_image_ocr` (when queued) -> `search_and_scrape_task` -> `ScraperService` persists results.

- HTMX component `price_table` -> hx-get `/dashboard/api/products/` -> `apps.dashboard.views.api_products` -> template `partials/product_rows.html` returned and inserted.

- `ScraperService.scrape()` calls into lower-level scraper client (SerpAPI wrapper) and returns standardized result dicts consumed by `persist_results()`.

Files of note:
- `apps/dashboard/views.py` — dashboard endpoints & HTMX handlers
- `apps/scraper/tasks.py` — background OCR + scraping tasks
- `apps/scraper/services/` — orchestrates scraping and persistence (ScraperService)
- `core/services/query_cleaner.py` — query normalization used by OCR and search
- `apps/scraper/models.py` — canonical DB model definitions
- `apps/accounts/models.py` — `User` / `Wallet` domain


**7) Dependency Overview**

Key frameworks & libraries used by the project:
- Django (project framework)
- HTMX (front-end partial updates via HTML attributes in templates)
- Pillow (`PIL`) — image processing
- pytesseract — Python wrapper for Tesseract OCR
- Tesseract OCR (system binary) — OCR engine
- Celery (and Redis broker) — background workers (also sometimes django-q in other parts)
- django-cotton (theme/style helper used in templates)
- django-allauth — authentication/social auth
- mysqlclient / Django MySQL backend if configured (or SQLite fallback)
- requests / SerpAPI client used within ScraperService
- numpy (used in trend calculations)


**8) System Architecture (textual diagram)**

Frontend (Django templates + HTMX + small JS)
  ↓ HTMX / Fetch / Forms
Django Views (apps.dashboard, apps.scraper, core)
  ↓ Calls
Service Layer (ScraperService, query_cleaner, persistence helpers)
  ↓ Writes/Reads
Database (MySQL or SQLite) — models: Product, StorePrice, PriceHistory, Watchlist, ProductImage, User, Wallet
  ↑
Background Workers (Celery / django-q)
  ↑
External Services: SerpAPI / Marketplace scrapers, Redis (Celery broker/result), Tesseract (local binary)


**9) Suggested Documentation Structure (create `PROJECT_ARCHITECTURE.md`)**

- Project Overview
- How to run locally (requirements, Tesseract install note, env vars)
- Backend Architecture (endpoints table + service mapping)
- Frontend Architecture (templates, components, HTMX triggers)
- Database Schema (models + relationships)
- Feature Workflows (OCR, Search, Watchlist, Alerts)
- Call Graph / File mappings
- Dependency list & where to find configuration
- Troubleshooting checklist (Tesseract not on PATH, Redis/Celery not configured, SerpAPI key missing)

---

Appendix: Quick troubleshooting notes
- Tesseract: confirm `TESSERACT_CMD` or add tesseract to system PATH. The code expects `settings.TESSERACT_CMD` may be set.
- Celery: ensure `redis` running on configured host and run `celery -A config worker --loglevel=info`.
- SerpAPI: set `SERPAPI_API_KEY` in env for scraping.


If you want, I can:
- produce a visual diagram (Mermaid) of the call flows, or
- write a short `DEVELOPER_GUIDE.md` with run steps, env vars, and test commands.

