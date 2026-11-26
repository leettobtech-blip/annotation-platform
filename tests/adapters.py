"""Local, deterministic fixtures for the adapter and provider HTTP contracts."""

from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI()


@app.post("/image")
@app.post("/document")
@app.post("/workflow")
@app.post("/video")
async def adapter(request: Request, idempotency_key: str = Header()):
    kind = request.url.path[1:]
    if request.headers.get("content-type", "").startswith("multipart/form-data;"):
        raw = await request.body()
        signatures = {"image": b"\x89PNG", "document": b"%PDF-", "video": b"ftyp"}
        if signatures[kind] not in raw or b'name="tenant_id"' not in raw or b'name="file"' not in raw:
            raise HTTPException(400)
        return {"title": f"Uploaded {kind}", "text": f"The {kind} contains an amber beacon."}
    payload = await request.json()
    if not {"tenant_id", "project_id"} <= payload.keys():
        raise HTTPException(400)
    if request.url.path == "/workflow":
        if payload["action"] != "review_requested":
            raise HTTPException(400)
        return {"workflow_id": idempotency_key}
    if payload["external_id"] != f"legacy-{kind}-42":
        raise HTTPException(404)
    return {"title": f"{kind} fixture", "text": f"The {kind} inspection found a sapphire valve."}


@app.post("/v1/audio/transcriptions")
async def transcribe(request: Request):
    raw = await request.body()
    if b"RIFF" not in raw or b"whisper-1" not in raw:
        raise HTTPException(400)
    return {"text": "The tape records a silver turbine."}


@app.post("/v1/chat/completions")
async def completion(request: Request):
    import json

    payload = await request.json()
    evidence = json.loads(payload["messages"][1]["content"])["evidence"]
    return {"choices": [{"message": {"content": evidence[0]["text"] + " [1]"}}]}
