# Codex handoff for VS Code

## Goal

Run the existing Annotation Platform locally and verify the browser flow. The user has Codex in VS Code and previously saw ERR_CONNECTION_REFUSED at localhost:3000. Open the directory containing this file and compose.yaml as the VS Code workspace.

## Local startup and diagnosis

- Confirm the VS Code terminal is on the user's computer and Docker Desktop or the Docker daemon is running.
- From this directory, run `docker compose -f compose.yaml -f deploy/compose.fixtures.yaml up --build`. In another terminal use the same two `-f` options with `ps -a` and `logs --tail=80 api migrate worker db fixtures` if startup stalls.
- Verify `http://127.0.0.1:3000/health/ready`, then open `http://127.0.0.1:3000/` and enter the local demo token `local-annotation-demo`.
- If port 3000 is occupied, set API_PORT to a free port using the local shell syntax and verify the resulting URL. Preserve existing Docker volumes and unrelated services.
- The README also describes running the API and worker directly with Python 3.12, FFmpeg and PostgreSQL.

## What exists and what was verified

- FastAPI serves the browser UI in annotation/ui and the API. PostgreSQL stores project and job state and indexes searchable text. Worker processes handle media, extraction and RAG.
- The UI uploads audio, video, documents and images, tracks processing, selects up to 20 sources, and displays cited answers and structured JSON.
- The fixture overlay is for local verification and returns deterministic sample results. Plain UTF-8 text is extracted directly. Connect actual OCR, transcription, video vision, document and chat providers before expecting real content extraction from those formats.
- PostgreSQL smoke checks passed for all four upload categories and file-scoped chat. The full Docker run was constrained by disk space in the earlier cloud environment. The browser click-through remained unverified because the preview relay timed out.
- For local verification, upload a TXT file containing a distinctive fact, select it, ask a matching question, and inspect Sources and Structured output (JSON). See tests/smoke.py for API coverage.
- One million concurrent users remains an unverified target. See deploy/production.md for capacity assumptions and staging checks.
