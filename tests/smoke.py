"""Run against a running API, worker and PostgreSQL: python tests/smoke.py."""

import os
from pathlib import Path
import subprocess
import tempfile
import time
from uuid import uuid4

import httpx

base_url = os.environ.get("API_URL", "http://127.0.0.1:3000")
token = os.environ.get("DEMO_TOKEN", "local-annotation-demo")
client = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
integration = os.environ.get("TEST_ADAPTERS") == "1"
project = f"/v1/projects/{uuid4()}"
other_project = f"/v1/projects/{uuid4()}"


def call(method, path, expected=200, **kwargs):
    response = client.request(method, path, **kwargs)
    assert response.status_code == expected, (method, path, response.status_code, response.text)
    return response


def submit(path, body=None, key=None, scope=project, expected=202):
    headers = {"Idempotency-Key": str(key or uuid4())}
    return call("POST", scope + path, expected, json=body, headers=headers).json()


def wait(job, scope=project, expected="succeeded"):
    for _ in range(120):
        state = call("GET", f"{scope}/jobs/{job['id']}").json()
        if state["status"] in {"failed", "succeeded"}:
            assert state["status"] == expected, state
            return state["result"]
        time.sleep(0.2)
    raise AssertionError(f"Job timed out: {job}")


def upload_bytes(binary, content_type):
    asset_id = uuid4()
    upload = call("PUT", f"{project}/media/{asset_id}", json={"content_type": content_type}).json()["upload"]
    call("PUT", upload["url"], 204, content=binary, headers={"Content-Type": content_type})
    endpoint = f"/assets/{asset_id}"
    details = wait(submit(endpoint + "/process"))
    if details.get("duration_ms"):
        response = call("GET", upload["url"], 206, headers={"Range": "bytes=0-11"})
        assert response.content == binary[:12]
    return str(asset_id), details


def main():
    call("GET", "/health/ready")
    page = call("GET", "/")
    assert 'id="picker"' in page.text and "/ui/workspace.js" in page.text
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    call("GET", "/ui/workspace.js")
    call("GET", "/ui/workspace.css")
    assert "application/pdf" in call("GET", "/upload-formats").json()["types"]
    call("PUT", project, json={"name": "Smoke project"})
    call("PUT", other_project, json={"name": "Isolated project"})
    assert httpx.get(base_url + project + f"/jobs/{uuid4()}").status_code == 401

    key = uuid4()
    source = {"title": "Manual", "text": "The quartz calibrator uses a cobalt relay."}
    index_job = submit("/sources", source, key)
    assert submit("/sources", source, key)["id"] == index_job["id"]
    submit("/sources", {**source, "title": "Different"}, key, expected=409)
    wait(index_job)
    call("GET", other_project + f"/jobs/{index_job['id']}", 404)

    chat_job = submit("/chat", {"question": "What does the quartz calibrator use?"})
    result = wait(chat_job)
    assert "cobalt relay" in result["answer"] and result["citations"][0]["metadata"]["title"] == "Manual"
    result = wait(submit("/chat", {"question": "And that?", "previous_turn": chat_job["id"]}))
    assert "cobalt relay" in result["answer"]
    result = wait(submit("/chat", {"question": "quartz"}, scope=other_project), scope=other_project)
    assert result["mode"] == "no_evidence"

    with tempfile.TemporaryDirectory() as directory:
        audio = Path(directory) / "test.wav"
        video = Path(directory) / "test.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440",
                        "-t", "1", str(audio)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=64x64:rate=10",
                        "-t", "1", "-c:v", "mpeg4", str(video)], check=True)
        audio_id, details = upload_bytes(audio.read_bytes(), "audio/wav")
        assert details["tracks"] == ["audio"]
        if integration:
            assert details["transcription"] == "indexed"
            result = wait(submit("/chat", {"question": "What turbine is on the tape?", "asset_ids": [audio_id]}))
            assert "silver turbine" in result["answer"]
        video_id, details = upload_bytes(video.read_bytes(), "video/mp4")
        assert details["tracks"] == ["video"]

    endpoint = f"{project}/assets/{video_id}/annotations"
    annotation = {"id": str(uuid4()), "label": "Rotor", "note": "Jade propeller", "start_ms": 5, "end_ms": 400}
    call("POST", endpoint, 422, json={**annotation, "end_ms": 1100})
    call("POST", endpoint, 201, json=annotation)
    call("POST", endpoint, 201, json=annotation)
    assert len(call("GET", endpoint).json()["items"]) == 1
    result = wait(submit("/chat", {"question": "Where is the jade propeller?"}))
    assert result["citations"][0]["metadata"]["end_ms"] == 400

    text_id, info = upload_bytes(b"The calibration checklist mentions a cobalt relay.", "text/plain")
    assert info["evidence"] == "indexed"
    scoped = submit("/chat", {"question": "What uses a cobalt relay?", "asset_ids": [text_id]})
    result = wait(scoped)
    assert {row["metadata"]["asset_id"] for row in result["citations"]} == {text_id}
    follow = wait(submit("/chat", {"question": "And that?", "previous_turn": scoped["id"]}))
    assert {row["metadata"]["asset_id"] for row in follow["citations"]} == {text_id}
    submit("/chat", {"question": "cobalt", "asset_ids": [text_id]}, scope=other_project, expected=404)

    if integration:
        import base64

        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII=")
        image_id, info = upload_bytes(png, "image/png")
        assert info["evidence"] == "indexed"
        document_id, info = upload_bytes(b"%PDF-1.7\n% fixture for the adapter transport\n", "application/pdf")
        assert info["evidence"] == "indexed"
        selected = [video_id, image_id, document_id]
        result = wait(submit("/chat", {"question": "Where is the amber beacon?", "asset_ids": selected}))
        assert {row["metadata"]["asset_id"] for row in result["citations"]} == set(selected)
        result = wait(submit("/chat", {"question": "Summarize this", "asset_ids": [image_id]}))
        assert "amber beacon" in result["answer"]
        for kind in ("image", "document"):
            asset = uuid4()
            call("PUT", f"{project}/external/{asset}", json={"kind": kind, "external_id": f"legacy-{kind}-42"})
            wait(submit(f"/assets/{asset}/process"))
        result = wait(submit("/chat", {"question": "What did the sapphire inspection find?"}))
        assert {row["metadata"]["type"] for row in result["citations"]} == {"image", "document"}
        workflow = wait(submit("/workflows", {"asset_id": audio_id, "action": "review_requested"}))
        assert workflow["workflow_id"]
    print("End-to-end checks passed" + (" with all fixture integrations" if integration else ""))


if __name__ == "__main__":
    main()
