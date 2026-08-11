# Etsy QQ Mail Order Backend

This project turns the original qq_idleCopy.py script into a backend service
with direct Etsy field extraction, QQ IMAP polling, SQLite persistence, JPEG
template previews, and Google Sheets delivery.
After a mailbox order image is generated, the backend also sends the image and
an order-number/product text message to the configured WeCom group robot.

## Run

Install dependencies:

    pip install -r requirements.txt

Start the backend:

    python run_backend.py

By default, the QQ mailbox listener starts together with the backend. Set AUTO_START_MAIL_LISTENER=false if you only want to expose the API without starting email monitoring.

Default URLs:

- API root: http://127.0.0.1:8000
- Swagger docs: http://127.0.0.1:8000/docs
- ReDoc: http://127.0.0.1:8000/redoc
- OpenAPI JSON: http://127.0.0.1:8000/openapi.json

## Authentication

Set BACKEND_API_TOKEN to protect all /api/v1/* endpoints. Frontend requests must then include X-API-Key: your-token.

If BACKEND_API_TOKEN is empty, local API endpoints are open.

## Main Endpoints

- GET /health - backend health, config validity, listener status.
- GET /api/v1/config - redacted runtime config and configured products.
- GET /api/v1/personalization-rules - compatibility endpoint reporting that rules are disabled.
- POST /api/v1/orders/parse - parse an email subject/body into order JSON.
- POST /api/v1/orders/publish - write an already parsed order to Google Sheets.
- POST /api/v1/orders/parse-and-publish - parse then write synchronously.
- POST /api/v1/orders/print-image - generate a printable A4 order JPEG and
  return it as Base64. The JSON body requires `order_id` and `order_number`.
- GET /api/v1/tasks/mail-listener - listener status.
- POST /api/v1/tasks/mail-listener/start - start continuous QQ IMAP listener.
- POST /api/v1/tasks/mail-listener/stop - stop continuous listener.
- POST /api/v1/tasks/mail-poll - run one asynchronous mailbox poll.
- POST /api/v1/tasks/orders/parse-and-publish - async parse/write task.
- GET /api/v1/tasks/jobs - list async jobs.
- GET /api/v1/tasks/jobs/{task_id} - inspect one job.

## Configuration

The code still has local defaults for compatibility, but every important value can be overridden with environment variables. See .env.example for the full list.

Default CORS origins include common local frontend ports 3000, 5173, 5174, and 5175 on both localhost and 127.0.0.1. If your frontend uses another origin, add it to CORS_ORIGINS and restart the backend.

Important files:

- qq_idleCopy.py - core mail parsing and integrations.
- backend/main.py - FastAPI app and HTTP endpoints.
- backend/task_manager.py - listener and async task management.
- Code.gs - Google Apps Script webhook used by APPS_SCRIPT_URL.

Template preview images generated for mailbox orders are saved as JPEG files
in `generated_template_jpgs/` by default. Use `TEMPLATE_JPG_DIR` to select a
different directory and `TEMPLATE_IMAGE_DPI` to change the output resolution.
Set `WECOM_ROBOT_WEBHOOK_URL` to override or disable the WeCom group robot
webhook. Images larger than WeCom's 2 MB limit are compressed in memory for the
notification; the generated JPEG on disk is not changed.

Printable order sheets use `ORDER_PRINT_IMAGE_DPI` (300 by default). Product
information is translated to Chinese with the configured DeepSeek API before
the sheet is rendered.

## Notes

- The continuous listener and one-time poll share qq_imap_state.json.
- Before product parsing, mailbox orders must match
  an existing row in the SQLite `shops` table. Missing shops are skipped with a
  `系统没有此店铺` console message and advance UID.
- Etsy option fields are extracted directly by their email labels. Wrapped
  lines under `Names/date/location for the cover` are joined into one value.
  Product titles are read from the text immediately before the first option
  field; the obsolete `规格/尺寸` field is not emitted or stored.
- Product option labels are not limited to a fixed list. The text between the
  product title and `Shop:` is parsed into the single `商品信息` object. SQLite
  stores it directly in `orders.product_information`; the order API restores
  the object for dynamic frontend columns. No `product_information_json`
  column or child information table is used.
- Before an order is inserted or updated, its shop and product name must match
  `size_template_products`. The resolved `size_template_id` is stored on the
  order and reused for template image generation. Unmatched products are not
  written to the orders table.
- SQLite stores order values under English field names. Each business field has
  a matching `<field_name>_text` column containing its Chinese display name.
  Legacy `personalization_json` and `personalization_text` columns are removed
  automatically when the order repository initializes.
- The former `personalization_rules.json` shop/product rules file has been
  removed. Mail product fields are parsed from the product block directly.
- Real QQ IMAP and Google Sheets calls happen in worker threads so HTTP requests do not block the event loop.
- Failed Google Sheets writes are retried from an in-memory background queue.
  The orders table contains no Google Sheets fields; restarting the process
  clears any pending retries.
