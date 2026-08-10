# Etsy QQ Mail Order Backend

This project turns the original qq_idleCopy.py script into a backend service while keeping the existing parsing, DeepSeek personalization matching, QQ IMAP polling, and Google Sheets write logic.

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
- GET /api/v1/personalization-rules - current JSON rules.
- POST /api/v1/orders/parse - parse an email subject/body into order JSON.
- POST /api/v1/orders/publish - write an already parsed order to Google Sheets.
- POST /api/v1/orders/parse-and-publish - parse then write synchronously.
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
- personalization_rules.json - shop/product-specific personalization rules.
- Code.gs - Google Apps Script webhook used by APPS_SCRIPT_URL.

## Notes

- The continuous listener and one-time poll share qq_imap_state.json.
- Unknown shop/product personalization rules are skipped and advance UID, matching the current script behavior.
- Real DeepSeek, QQ IMAP, and Google Sheets calls happen in worker threads so HTTP requests do not block the event loop.
