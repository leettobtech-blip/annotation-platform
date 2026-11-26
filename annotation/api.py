from contextlib import asynccontextmanager
import os
from pathlib import Path
from time import perf_counter
from typing import Annotated, Literal
from uuid import UUID, uuid4

from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from prometheus_client import Counter, Histogram, make_asgi_app
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.staticfiles import StaticFiles

from . import storage
from .auth import Principal, check_config, permission
from .db import pool
from .formats import FORMATS

Reader = Annotated[Principal, Depends(permission("annotation:read"))]
Writer = Annotated[Principal, Depends(permission("annotation:write"))]
IdempotencyKey = Annotated[UUID, Header()]


class Project(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class Upload(BaseModel):
    content_type: str = Field(max_length=150, json_schema_extra={"enum": list(FORMATS)})


class ExternalAsset(BaseModel):
    kind: Literal["image", "document"]
    external_id: str = Field(min_length=1, max_length=250)


class Annotation(BaseModel):
    id: UUID
    label: str = Field(min_length=1, max_length=100)
    start_ms: int = Field(ge=0, le=86_400_000)
    end_ms: int = Field(gt=0, le=86_400_000)
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must exceed start_ms")
        return self


class Source(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=40_000)


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    previous_turn: UUID | None = None
    asset_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=20)


class Workflow(BaseModel):
    asset_id: UUID
    action: Literal["review_requested", "annotation_completed"]


@asynccontextmanager
async def lifespan(app):
    check_config()
    await run_in_threadpool(lambda: pool.open(wait=True, timeout=10))
    try:
        yield
    finally:
        await run_in_threadpool(pool.close)


http_requests = Counter("annotation_http_requests_total", "HTTP responses", ("method", "route", "status"))
http_duration = Histogram("annotation_http_duration_seconds", "HTTP request handling time", ("method", "route"))


app = FastAPI(title="Annotation Platform", version="1.0.0", lifespan=lifespan)
if os.getenv("ENABLE_METRICS", "1") == "1":
    app.mount("/metrics", make_asgi_app())


@app.middleware("http")
async def limit_json(request: Request, call_next):
    if request.method in {"POST", "PUT"} and not request.url.path.endswith("/content"):
        size = request.headers.get("content-length", "")
        if not size.isascii() or not size.isdecimal() or len(size) > 8:
            return JSONResponse({"detail": "Content-Length required"}, status_code=411)
        if int(size) > 250_000:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
    return await call_next(request)


@app.middleware("http")
async def measure_requests(request: Request, call_next):
    started = perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        method = request.method if request.method in {"GET", "HEAD", "POST", "PUT", "OPTIONS"} else "other"
        http_requests.labels(method, route, status).inc()
        http_duration.labels(method, route).observe(perf_counter() - started)


@app.exception_handler(RequestValidationError)
async def reject_invalid_input(request, exc):
    issues = [{"location": item["loc"], "type": item["type"]} for item in exc.errors()]
    return JSONResponse({"detail": issues}, status_code=422)


@app.exception_handler(PoolTimeout)
async def busy(request, exc):
    return JSONResponse({"detail": "Service busy"}, status_code=503, headers={"Retry-After": "2"})


ui_directory = Path(__file__).with_name("ui")
app.mount("/ui", StaticFiles(directory=ui_directory), name="ui")


@app.get("/", include_in_schema=False)
def workspace():
    return FileResponse(ui_directory / "index.html", headers={
        "Content-Security-Policy": (
            "default-src 'self'; connect-src 'self' https:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/upload-formats", include_in_schema=False)
def upload_formats():
    return {"types": FORMATS, "max_bytes": storage.max_bytes}


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    with pool.connection() as conn:
        conn.execute("SELECT id FROM projects LIMIT 0")
    return {"status": "ok"}


def require_project(conn, tenant, project):
    if not conn.execute("SELECT 1 FROM projects WHERE tenant=%s AND id=%s", (tenant, project)).fetchone():
        raise HTTPException(404, "Project not found")


def asset_record(conn, tenant, project, asset):
    row = conn.execute("SELECT id, kind, content_type, external_id, object_version, duration_ms FROM assets "
                       "WHERE tenant=%s AND project=%s AND id=%s", (tenant, project, asset)).fetchone()
    if row is None:
        raise HTTPException(404, "Asset not found")
    return row


def enqueue(conn, tenant, project, kind, idem, payload):
    row = conn.execute(
        """INSERT INTO jobs (id, tenant, project, kind, idem, request)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (tenant, project, kind, idem) DO NOTHING
           RETURNING id, status, request""",
        (uuid4(), tenant, project, kind, idem, Jsonb(payload)),
    ).fetchone()
    if row is None:
        row = conn.execute("SELECT id,status,request FROM jobs WHERE tenant=%s AND project=%s AND kind=%s AND idem=%s",
                           (tenant, project, kind, idem)).fetchone()
        if row["request"] != payload:
            raise HTTPException(409, "Idempotency key reused with a different request")
    return {"id": row["id"], "status": row["status"]}


@app.put("/v1/projects/{project}")
def create_project(project: UUID, body: Project, user: Writer):
    with pool.connection() as conn:
        conn.execute("INSERT INTO projects VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                     (user.tenant, project, body.name))
        row = conn.execute("SELECT name FROM projects WHERE tenant=%s AND id=%s",
                           (user.tenant, project)).fetchone()
        if row["name"] != body.name:
            raise HTTPException(409, "Project ID already exists")
    return {"id": project, "name": body.name}


@app.put("/v1/projects/{project}/media/{asset}")
def create_upload(project: UUID, asset: UUID, body: Upload, user: Writer):
    if body.content_type not in FORMATS:
        raise HTTPException(415, "Unsupported upload type")
    kind, _ = FORMATS[body.content_type]
    with pool.connection() as conn:
        require_project(conn, user.tenant, project)
        conn.execute("INSERT INTO assets (tenant,project,id,kind,content_type,external_id) VALUES (%s,%s,%s,%s,%s,NULL) ON CONFLICT DO NOTHING",
                     (user.tenant, project, asset, kind, body.content_type))
        row = asset_record(conn, user.tenant, project, asset)
        if row["content_type"] != body.content_type:
            raise HTTPException(409, "Asset ID already exists")
    object_key = storage.key(user.tenant, asset, project)
    ticket = storage.upload_ticket(object_key, body.content_type) if storage.bucket else {
        "url": f"/v1/projects/{project}/media/{asset}/content", "method": "PUT"
    }
    return {"id": asset, "upload": ticket}


@app.put("/v1/projects/{project}/media/{asset}/content", status_code=204)
async def upload_content(project: UUID, asset: UUID, request: Request, user: Writer):
    if storage.bucket:
        raise HTTPException(404)

    def lookup():
        with pool.connection() as conn:
            return asset_record(conn, user.tenant, project, asset)

    row = await run_in_threadpool(lookup)
    if row["external_id"] is not None or request.headers.get("content-type") != row["content_type"]:
        raise HTTPException(415, "Media type mismatch")
    path = storage.media_root / storage.key(user.tenant, asset, project)
    await run_in_threadpool(lambda: path.parent.mkdir(parents=True, exist_ok=True))
    pending = path.with_name(f".{uuid4()}")
    size = 0
    try:
        with pending.open("xb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > storage.max_bytes:
                    raise HTTPException(413, "Media too large")
                await run_in_threadpool(output.write, chunk)
            if size == 0:
                raise HTTPException(422, "Empty media")
        try:
            await run_in_threadpool(os.link, pending, path)
        except FileExistsError:
            raise HTTPException(409, "Media already uploaded") from None
    finally:
        pending.unlink(missing_ok=True)


@app.get("/v1/projects/{project}/media/{asset}/content")
def playback(project: UUID, asset: UUID, user: Reader):
    with pool.connection() as conn:
        row = asset_record(conn, user.tenant, project, asset)
    if row["duration_ms"] is None:
        raise HTTPException(409, "Process the media before playback")
    object_key = storage.key(user.tenant, asset, project)
    if storage.bucket:
        return RedirectResponse(storage.playback_url(object_key, row["object_version"]),
                                headers={"Cache-Control": "no-store"})
    return FileResponse(storage.media_root / object_key, media_type=row["content_type"],
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@app.put("/v1/projects/{project}/external/{asset}")
def register_external(project: UUID, asset: UUID, body: ExternalAsset, user: Writer):
    with pool.connection() as conn:
        require_project(conn, user.tenant, project)
        conn.execute("INSERT INTO assets (tenant,project,id,kind,content_type,external_id) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                     (user.tenant, project, asset, body.kind, "external", body.external_id))
        row = asset_record(conn, user.tenant, project, asset)
        if (row["kind"], row["external_id"]) != (body.kind, body.external_id):
            raise HTTPException(409, "Asset ID already exists")
    return {"id": row["id"], "kind": row["kind"], "external_id": row["external_id"]}


@app.post("/v1/projects/{project}/assets/{asset}/process", status_code=202)
def process_asset(project: UUID, asset: UUID, idempotency_key: IdempotencyKey, user: Writer):
    with pool.connection() as conn:
        row = asset_record(conn, user.tenant, project, asset)
    payload = {"asset": str(asset), "external_id": row["external_id"]}
    uploaded = row["external_id"] is None
    if uploaded and row["kind"] not in {"audio", "video"}:
        payload["content_type"] = row["content_type"]
    object_key = storage.key(user.tenant, asset, project)
    if uploaded and not storage.bucket and not (storage.media_root / object_key).is_file():
        raise HTTPException(409, "Upload the file before processing")
    is_remote_media = uploaded and storage.bucket
    if is_remote_media and not row["object_version"]:
        try:
            payload["version"] = storage.current_version(object_key)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise HTTPException(409, "Upload the media before processing") from None
            raise HTTPException(503, "Media storage unavailable") from None
        except ValueError:
            raise HTTPException(503, "Media storage is missing versioning") from None
    with pool.connection() as conn:
        if is_remote_media:
            pinned = conn.execute(
                """UPDATE assets SET object_version=COALESCE(object_version,%s)
                   WHERE tenant=%s AND project=%s AND id=%s RETURNING object_version""",
                (payload.get("version"), user.tenant, project, asset),
            ).fetchone()
            payload["version"] = pinned["object_version"]
        return enqueue(conn, user.tenant, project, row["kind"], idempotency_key, payload)


@app.post("/v1/projects/{project}/assets/{asset}/annotations", status_code=201)
def annotate(project: UUID, asset: UUID, body: Annotation, user: Writer):
    with pool.connection() as conn:
        row = asset_record(conn, user.tenant, project, asset)
        if row["kind"] not in {"audio", "video"}:
            raise HTTPException(422, "Temporal annotations require audio or video")
        if row["duration_ms"] is None:
            raise HTTPException(409, "Process the media before annotating")
        if body.end_ms > row["duration_ms"]:
            raise HTTPException(422, "Annotation exceeds media duration")
        data = (asset, user.subject, body.label, body.start_ms, body.end_ms, body.note)
        created = conn.execute(
            """INSERT INTO annotations VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT DO NOTHING RETURNING id""",
            (user.tenant, project, body.id, *data),
        ).fetchone()
        if not created:
            existing = conn.execute(
                "SELECT asset,author,label,start_ms,end_ms,note FROM annotations WHERE tenant=%s AND project=%s AND id=%s",
                (user.tenant, project, body.id),
            ).fetchone()
            if tuple(existing.values()) != data:
                raise HTTPException(409, "Annotation ID already exists")
        else:
            conn.execute(
                "INSERT INTO chunks VALUES (%s,%s,%s,'annotation',0,%s,%s)",
                (user.tenant, project, body.id, f"{body.label}\n{body.note}",
                 Jsonb({"type": "annotation", "asset_id": str(asset), "start_ms": body.start_ms, "end_ms": body.end_ms})),
            )
    return body


@app.get("/v1/projects/{project}/assets/{asset}/annotations")
def list_annotations(project: UUID, asset: UUID, user: Reader, after: UUID = UUID(int=0),
                     limit: int = Query(50, ge=1, le=100)):
    with pool.connection() as conn:
        asset_record(conn, user.tenant, project, asset)
        items = conn.execute(
            """SELECT id,author,label,start_ms,end_ms,note FROM annotations
               WHERE tenant=%s AND project=%s AND asset=%s AND id>%s ORDER BY id LIMIT %s""",
            (user.tenant, project, asset, after, limit + 1),
        ).fetchall()
    return {"items": items[:limit], "next": items[limit - 1]["id"] if len(items) > limit else None}


@app.post("/v1/projects/{project}/sources", status_code=202)
def index_source(project: UUID, body: Source, idempotency_key: IdempotencyKey, user: Writer):
    with pool.connection() as conn:
        require_project(conn, user.tenant, project)
        return enqueue(conn, user.tenant, project, "index", idempotency_key, body.model_dump())


@app.post("/v1/projects/{project}/chat", status_code=202)
def ask(project: UUID, body: Question, idempotency_key: IdempotencyKey, user: Writer):
    with pool.connection() as conn:
        require_project(conn, user.tenant, project)
        selected = sorted({str(asset) for asset in body.asset_ids}) if body.asset_ids else None
        if body.previous_turn:
            previous = conn.execute(
                "SELECT request FROM jobs WHERE id=%s AND tenant=%s AND project=%s AND kind='chat' "
                "AND status='succeeded' AND request->>'author'=%s",
                (body.previous_turn, user.tenant, project, user.subject),
            ).fetchone()
            if not previous:
                raise HTTPException(404, "Completed chat turn not found")
            previous_selection = previous["request"].get("asset_ids")
            if selected is not None and selected != previous_selection:
                raise HTTPException(409, "Start a new turn to change the selected files")
            selected = previous_selection
        payload = {"question": body.question, "previous_turn": str(body.previous_turn) if body.previous_turn else None,
                   "author": user.subject}
        if selected:
            found = conn.execute(
                "SELECT count(*) AS total FROM assets WHERE tenant=%s AND project=%s AND id=ANY(%s::uuid[])",
                (user.tenant, project, selected),
            ).fetchone()
            if found["total"] != len(selected):
                raise HTTPException(404, "Selected file not found")
            payload["asset_ids"] = selected
        return enqueue(conn, user.tenant, project, "chat", idempotency_key, payload)


@app.post("/v1/projects/{project}/workflows", status_code=202)
def workflow(project: UUID, body: Workflow, idempotency_key: IdempotencyKey, user: Writer):
    with pool.connection() as conn:
        asset_record(conn, user.tenant, project, body.asset_id)
        return enqueue(conn, user.tenant, project, "workflow", idempotency_key, body.model_dump(mode="json"))


@app.get("/v1/projects/{project}/jobs/{job}")
def job_status(project: UUID, job: UUID, user: Reader):
    with pool.connection() as conn:
        row = conn.execute(
            """SELECT id,kind,status,attempts,error,result FROM jobs
               WHERE tenant=%s AND project=%s AND id=%s AND (kind<>'chat' OR request->>'author'=%s)""",
            (user.tenant, project, job, user.subject),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Job not found")
        return row
