# Annotation Platform

A backend for uploading audio, video, documents and images, asking questions about one or more uploaded files, and adding timestamped audio and video annotations. Extraction and workflow services connect through HTTP adapters. The interactive API is available at `/docs`.

## Run locally

Requires Docker with Compose.

```sh
docker compose up --build -d
curl -fsS http://localhost:3000/health/ready
```

Open `http://localhost:3000/` for the frontend and enter `local-annotation-demo` as the access token. The interactive API is at `/docs`. This binds the API to loopback and uses shared local media storage and a demo identity. Set `API_PORT` if port 3000 is occupied. PostgreSQL and media persist in named volumes.

To run the entire workflow with deterministic HTTP fixtures for the existing services, transcription and the LLM:

```sh
docker compose -f compose.yaml -f deploy/compose.fixtures.yaml up --build -d
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
TEST_ADAPTERS=1 .venv/bin/python tests/smoke.py
```

The host-side smoke runner needs FFmpeg. With the standard stack, omit `TEST_ADAPTERS=1` to test media annotation and extractive chat. The fixture configuration is for local verification.

### Run against an existing PostgreSQL server

With Python 3.12, FFmpeg and a PostgreSQL database:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
export DATABASE_URL=postgresql://annotation:annotation@127.0.0.1:5432/annotation
export DEMO_TOKEN=local-annotation-demo
.venv/bin/python -m annotation.db
```

With the same environment in two terminals, run `.venv/bin/uvicorn annotation.api:app --port 3000` and `.venv/bin/python -m annotation.worker`.

## Use the browser workspace

1. Enter an API access token and choose **Create workspace**.
2. Choose or drop files into **Source files**. Wait for **Ready to ask**.
3. Select files to narrow the search, or leave the checkboxes clear to search the project. Ask a question and expand **Sources** or **Structured output (JSON)** to inspect the result.
4. Use **New conversation** to change the source selection.

The frontend is served by FastAPI and uses its existing per-tenant access controls. The access token, file list, and conversation live in the current browser page. Refreshing creates a fresh workspace when you sign in again. Backend data remains in PostgreSQL and storage. For direct browser uploads to S3, allow multipart POST from the frontend origin in the bucket CORS policy.

## Workflow

1. Create a project with `PUT /v1/projects/{project}` and a client-generated UUID. Every remaining path below starts with that prefix.
2. Register a file of any supported type with `PUT /media/{asset}`. In local mode, send bytes to the returned PUT URL with the declared content type. With `MEDIA_BUCKET` set, submit the returned S3 form fields and file using multipart POST.
3. Submit `POST /assets/{asset}/process` with a UUID in the `Idempotency-Key` header. Poll `GET /jobs/{job}` until `succeeded` or `failed`. Successful media jobs return validated duration and track types. `GET /media/{asset}/content` supports byte ranges locally and issues a short-lived signed redirect on S3.
4. Create timestamp annotations with `POST /assets/{asset}/annotations`. Send `id`, `label`, `start_ms`, `end_ms`, and optional `note`. The same URL supports paginated retrieval. Bounds are checked against the processed duration. Labels and notes become searchable.
5. Add reference text with `POST /sources`, or register a legacy asset using `PUT /external/{asset}` and submit its process job. Audio text is indexed when a transcription provider is configured. Workflows may be dispatched with `POST /workflows`.
6. Once processing succeeds, send `POST /chat` with a question and optional `asset_ids`. Select up to 20 files, or omit `asset_ids` to search the project. Poll the resulting job for the answer and citations. Citations include asset IDs and any available annotation timestamps. Set `previous_turn` to continue with the same file selection. Start a new turn to change it.

```json
{
  "question": "What do these files say about the valve?",
  "asset_ids": ["69841b03-9072-43ca-9de9-56f071f1fc41", "0a262588-adb8-460c-9593-1c2c41a69ad5"]
}
```

### Supported uploads

| Category | Accepted formats | Searchable evidence |
| --- | --- | --- |
| Audio | WAV, MP3, M4A, Ogg, WebM | Transcription and manual annotations |
| Video | MP4, WebM, MOV, MKV | Transcription, visual descriptions, and manual annotations |
| Documents | PDF, Word, Excel, PowerPoint, ODT, UTF-8 text, CSV, Markdown | Extracted text |
| Images | JPEG, PNG, WebP, GIF, TIFF, BMP, HEIC | OCR text or image descriptions |

Uploads are limited to 128 MiB. The API schema lists exact MIME values. Decoding and extraction coverage depend on the configured adapters. Successful extraction with no readable text reports `no_text_found`. The service indexes up to the first 40,000 characters of each extraction.

Every asynchronous POST accepts an idempotency key scoped by tenant, project and job kind. Reusing a key for different input returns HTTP 409. Workers use expiring leases, conditional writes and bounded retries. The API scopes access to authenticated tenants. Within a tenant, authorized users share projects and sources. Chat turns are scoped to their author.

## Integrate existing services

Set these variables on the worker. URLs identify adapters that can translate the following contract to your existing endpoints.

| Setting | Request JSON fields | Successful response |
| --- | --- | --- |
| `IMAGE_ADAPTER_URL`, `DOCUMENT_ADAPTER_URL` | `tenant_id`, `project_id`, `asset`, `external_id` | `{"title":"…","text":"…"}` |
| `WORKFLOW_ADAPTER_URL` | `tenant_id`, `project_id`, `asset_id`, `action` | `{"workflow_id":"…"}` |

All three use POST with a stable job UUID in the `Idempotency-Key` header. Workflow adapters must deduplicate retries and scope external asset access to the supplied tenant and project. Use TLS and the corresponding `IMAGE_ADAPTER_TOKEN`, `DOCUMENT_ADAPTER_TOKEN`, or `WORKFLOW_ADAPTER_TOKEN`. The acknowledgment records dispatch. Subsequent workflow execution and status belong to the existing service.

For direct image and binary document uploads, the worker sends multipart POST to the same image and document adapter URLs. Accept form fields `tenant_id`, `project_id`, `asset`, and `content_type`, plus a `file` part. The worker supplies a filename based on the asset ID and MIME type. Return the same `title` and `text` JSON as for legacy assets. UTF-8 TXT, CSV, and Markdown use the built-in reader. Use an extraction service that validates the file bytes and runs parsers with resource limits.

Set `VIDEO_ADAPTER_URL` and optionally `VIDEO_ADAPTER_TOKEN` for questions about visuals, including silent clips. The video adapter accepts the same multipart contract and returns descriptions or extracted onscreen text. It runs alongside the optional audio transcription.

Set `TRANSCRIBE_BASE_URL`, `TRANSCRIBE_TOKEN`, and optionally `TRANSCRIBE_MODEL` for an OpenAI-compatible transcription endpoint. The worker extracts a mono WAV from clips through 10 minutes and indexes the returned text. Longer files support playback and manual annotation, plus visual extraction for video when configured. Set `CHAT_BASE_URL`, `CHAT_TOKEN`, and `CHAT_MODEL` for an OpenAI-compatible generation endpoint. Without it, chat returns cited excerpts. PostgreSQL full-text search retrieves lexical matches within each project.

## Production configuration

- Set `OIDC_ISSUER`, `OIDC_AUDIENCE`, and HTTPS `OIDC_JWKS_URL` on the API. Tokens require RS256, `exp`, `sub`, a UUID `tenant_id`, and space-delimited `annotation:read` and/or `annotation:write` scopes. Remove the demo token.
- Set `DATABASE_URL` with TLS, size `DB_POOL_SIZE`, and run `python -m annotation.db` before the API and workers, including upgrades from the earlier project version. Future schema changes should use versioned migrations.
- Set `MEDIA_BUCKET` and use AWS workload credentials for shared media. Enable bucket versioning. The service pins the first processed object version to the asset. Treat upload tickets and signed playback URLs as bearer credentials.
- See [production deployment and capacity validation](deploy/production.md) for isolation, scaling and the included load test. The one-million-user target requires capacity testing on the intended infrastructure.

## Source layout

- `annotation/api.py` and `annotation/auth.py` implement the API and identity checks.
- `annotation/worker.py` and `annotation/services.py` implement async processing, media, adapters and RAG.
- `annotation/schema.sql` defines PostgreSQL storage, indexing and queue state.
- `tests/` contains smoke verification and HTTP fixture integrations.
- `deploy/` contains the fixture overlay, capacity runbook, queue metrics query and k6 load script.
