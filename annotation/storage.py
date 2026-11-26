import os
from functools import lru_cache
from pathlib import Path

import boto3
from botocore.config import Config

bucket = os.getenv("MEDIA_BUCKET")
media_root = Path(os.getenv("MEDIA_ROOT", "./data")).resolve()
max_bytes = 128 * 1024 * 1024


@lru_cache
def client():
    return boto3.client("s3", config=Config(connect_timeout=3, read_timeout=15, signature_version="s3v4"))


def key(tenant, asset, project):
    return f"media/{tenant}/{project}/{asset}"


def upload_ticket(object_key, content_type):
    return client().generate_presigned_post(
        bucket, object_key, Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, max_bytes]],
        ExpiresIn=300,
    )


def materialize(object_key, path, version):
    if not bucket:
        source = media_root / object_key
        if source.stat().st_size > max_bytes:
            raise ValueError("Media too large")
        return source
    response = client().get_object(Bucket=bucket, Key=object_key, VersionId=version)
    with response["Body"] as body:
        if not 0 < response["ContentLength"] <= max_bytes:
            raise ValueError("Invalid media size")
        with path.open("wb") as target:
            size = 0
            for chunk in body.iter_chunks(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("Media too large")
                target.write(chunk)
    return path


def current_version(object_key):
    response = client().head_object(Bucket=bucket, Key=object_key)
    version = response.get("VersionId")
    if not version or version == "null":
        raise ValueError("Enable S3 bucket versioning")
    return version


def playback_url(object_key, version):
    return client().generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": object_key, "VersionId": version},
        ExpiresIn=120,
    )
