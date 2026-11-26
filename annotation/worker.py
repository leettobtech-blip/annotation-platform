import argparse
import logging
import os
import re
import signal
from threading import Event
from uuid import uuid4

import httpx
from psycopg import Error
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout

from .db import pool
from .services import PermanentFailure, adapter, answer, media, passages

log = logging.getLogger("annotation.worker")
kinds = ("audio", "video", "image", "document", "index", "chat", "workflow")
max_attempts = 4
stopped = Event()
stop_words = set("a an and are did does for from how i in is it me of on or please tell the this to was what when where which who why with you".split())


class DeadlineExceeded(Exception):
    pass


def deadline(signum, frame):
    raise DeadlineExceeded


def claim(selected):
    with pool.connection() as conn:
        row = conn.execute(
            """SELECT * FROM jobs WHERE kind=ANY(%s) AND status IN ('queued','running')
               AND available<=now() ORDER BY available,id LIMIT 1 FOR UPDATE SKIP LOCKED""",
            (selected,),
        ).fetchone()
        if not row:
            return None
        if row["attempts"] >= max_attempts:
            conn.execute("UPDATE jobs SET status='failed',error='Retry limit reached',lease=NULL WHERE id=%s",
                         (row["id"],))
            return None
        row["lease"] = uuid4()
        row["attempts"] += 1
        conn.execute(
            """UPDATE jobs SET status='running',attempts=%s,lease=%s,available=now()+interval '180 seconds'
               WHERE id=%s""",
            (row["attempts"], row["lease"], row["id"]),
        )
        return row


def history_for(conn, job):
    previous = job["request"]["previous_turn"]
    history = []
    for _ in range(5):
        if not previous:
            break
        row = conn.execute(
            """SELECT request,result FROM jobs WHERE id=%s AND tenant=%s AND project=%s AND kind='chat'
               AND status='succeeded' AND request->>'author'=%s""",
            (previous, job["tenant"], job["project"], job["request"]["author"]),
        ).fetchone()
        if not row or row["request"].get("asset_ids") != job["request"].get("asset_ids"):
            break
        history.append({"question": row["request"]["question"], "answer": row["result"]["answer"][:1200]})
        previous = row["request"]["previous_turn"]
    return history[::-1]


def tokens(question):
    return [word for word in re.findall(r"[^\W_]{2,40}", question.lower()) if word not in stop_words]


def chat(client, job):
    question = job["request"]["question"]
    selected = job["request"].get("asset_ids")
    with pool.connection() as conn:
        history = history_for(conn, job)
        search_words = tokens(question)
        if history and len(search_words) <= 2:
            search_words.extend(tokens(history[-1]["question"]))
        query = " OR ".join(dict.fromkeys(f'"{word}"' for word in search_words[:24]))
        overview = selected and re.match(
            r"^\s*(?:please\s+)?(?:summari[sz]e|give me (?:a summary|an overview)|what (?:is|are) .* about)\b",
            question, re.IGNORECASE,
        )
        if overview:
            sources = conn.execute(
                """SELECT source::text AS source_id,position,body,metadata
                   FROM chunks WHERE tenant=%s AND project=%s AND metadata->>'asset_id'=ANY(%s::text[])
                   ORDER BY position,source_kind,source LIMIT 12""",
                (job["tenant"], job["project"], selected),
            ).fetchall()
        else:
            sources = conn.execute(
                """SELECT source::text AS source_id,position,body,metadata
                   FROM chunks, websearch_to_tsquery('simple',%s) AS q
                   WHERE tenant=%s AND project=%s AND terms @@ q
                     AND (%s::text[] IS NULL OR metadata->>'asset_id'=ANY(%s::text[]))
                   ORDER BY ts_rank_cd(terms,q) DESC, source_kind,source,position LIMIT 5""",
                (query, job["tenant"], job["project"], selected, selected),
            ).fetchall() if query else []
    for number, source in enumerate(sources, 1):
        source["reference"] = f"[{number}]"
    return answer(client, question, history, sources), []


def execute(client, job):
    kind = job["kind"]
    if kind == "chat":
        return chat(client, job)
    if kind == "video" and os.getenv("VIDEO_ADAPTER_URL"):
        with pool.connection() as conn:
            asset = conn.execute(
                "SELECT content_type FROM assets WHERE tenant=%s AND project=%s AND id=%s",
                (job["tenant"], job["project"], job["request"]["asset"]),
            ).fetchone()
        job = {**job, "request": {**job["request"], "content_type": asset["content_type"]}}
    if kind in {"audio", "video"}:
        return media(client, job)
    if kind == "index":
        data = job["request"]
        return {"title": data["title"]}, passages(data["text"], {"type": "source", "title": data["title"]})
    return adapter(client, job)


def finish(job, result, chunks, error=None, retry=False):
    with pool.connection() as conn:
        if error:
            status = "queued" if retry and job["attempts"] < max_attempts else "failed"
            conn.execute(
                """UPDATE jobs SET status=%s,error=%s,lease=NULL,available=now()+%s*interval '1 second'
                   WHERE id=%s AND lease=%s""",
                (status, error, min(60, 2 ** job["attempts"]), job["id"], job["lease"]),
            )
            return
        row = conn.execute(
            """UPDATE jobs SET status='succeeded',result=%s,lease=NULL,error=NULL
               WHERE id=%s AND lease=%s RETURNING id""",
            (Jsonb(result), job["id"], job["lease"]),
        ).fetchone()
        if row and job["kind"] in {"audio", "video"}:
            conn.execute(
                "UPDATE assets SET duration_ms=%s WHERE tenant=%s AND project=%s AND id=%s",
                (result["duration_ms"], job["tenant"], job["project"], job["request"]["asset"]),
            )
        if row and chunks:
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO chunks VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    [(job["tenant"], job["project"], job["id"], job["kind"], i, body, Jsonb(metadata))
                     for i, (body, metadata) in enumerate(chunks)],
                )


def handle(client, job):
    try:
        signal.alarm(140)
        result, chunks = execute(client, job)
    except PermanentFailure as exc:
        finish(job, None, [], error=str(exc))
    except Exception as exc:
        log.warning("Job processing failed id=%s kind=%s exception=%s", job["id"], job["kind"], type(exc).__name__)
        finish(job, None, [], error="Processing failed", retry=True)
    else:
        finish(job, result, chunks)
    finally:
        signal.alarm(0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", action="append", choices=kinds)
    args = parser.parse_args()
    if os.getenv("CHAT_BASE_URL") and not os.getenv("CHAT_MODEL"):
        raise RuntimeError("CHAT_MODEL is required when CHAT_BASE_URL is configured")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGALRM, deadline)
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    pool.open(wait=True, timeout=10)
    try:
        with httpx.Client(timeout=httpx.Timeout(35, connect=4), follow_redirects=False) as client:
            while not stopped.is_set():
                try:
                    job = claim(args.kind or list(kinds))
                    if job:
                        handle(client, job)
                    else:
                        stopped.wait(0.5)
                except (Error, PoolTimeout, DeadlineExceeded) as exc:
                    log.warning("Worker operation interrupted exception=%s", type(exc).__name__)
                    stopped.wait(2)
    finally:
        pool.close()


if __name__ == "__main__":
    main()
