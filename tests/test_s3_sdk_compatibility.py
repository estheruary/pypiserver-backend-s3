"""Exercise real boto3 transfer and serialization paths without AWS traffic."""

import base64
from datetime import datetime, timezone
import hashlib
from io import BytesIO
from types import SimpleNamespace

from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber
import pytest

from pypiserver_backend_s3.cached_backend import CachedS3Backend


@pytest.fixture
def backend():
    return CachedS3Backend(
        SimpleNamespace(
            hash_algo="sha256",
            backend_args={
                "bucket": "test-bucket",
                "prefix": "packages/",
                "default_region": "us-east-2",
                "access_key": "test",
                "secret_access_key": "test",
            },
        )
    )


def test_sdk_listing_and_native_hash(backend):
    hasher = hashlib.sha256(b"wheel")
    obj = {
        "Key": "packages/example-1.0.tar.gz",
        "Size": 5,
        "ETag": '"opaque-etag"',
        "LastModified": datetime.now(timezone.utc),
        "ChecksumAlgorithm": ["SHA256"],
        "ChecksumType": "FULL_OBJECT",
    }
    with Stubber(backend.s3_client) as stub:
        stub.add_response(
            "list_objects_v2",
            {"Contents": [obj]},
            {"Bucket": "test-bucket", "Prefix": "packages/"},
        )
        stub.add_response(
            "head_object",
            {
                "ContentLength": 5,
                "ETag": obj["ETag"],
                "LastModified": obj["LastModified"],
                "ChecksumSHA256": base64.b64encode(hasher.digest()).decode(),
                "ChecksumType": "FULL_OBJECT",
            },
            {"Bucket": "test-bucket", "Key": obj["Key"], "ChecksumMode": "ENABLED"},
        )
        pkg = next(backend.get_all_packages())
        assert backend.digest(pkg) == f"sha256={hasher.hexdigest()}"
        assert backend.digest(next(backend.get_all_packages())) == f"sha256={hasher.hexdigest()}"
        stub.assert_no_pending_responses()


def test_sdk_upload_preserves_hash_metadata(backend):
    data = b"new package bytes"
    with Stubber(backend.s3_client) as stub:
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": "test-bucket",
                "Key": "packages/example-1.0.tar.gz",
                "Body": ANY,
                "Metadata": {"pypiserver-sha256": hashlib.sha256(data).hexdigest()},
                "ChecksumAlgorithm": "CRC32",
            },
        )
        backend.add_package("example-1.0.tar.gz", BytesIO(data))
        stub.assert_no_pending_responses()


def test_sdk_download_and_delete(backend):
    key = "packages/example-1.0.tar.gz"
    data = b"package bytes"
    with Stubber(backend.s3_client) as stub:
        stub.add_response(
            "head_object",
            {"ContentLength": len(data)},
            {"Bucket": "test-bucket", "Key": key},
        )
        stub.add_response(
            "get_object",
            {
                "ContentLength": len(data),
                "Body": StreamingBody(BytesIO(data), len(data)),
            },
            {"Bucket": "test-bucket", "Key": key},
        )
        with backend.package("example-1.0.tar.gz") as stream:
            assert stream.read() == data
        stub.add_response("delete_object", {}, {"Bucket": "test-bucket", "Key": key})
        backend.remove_package(SimpleNamespace(relfn="example-1.0.tar.gz"))
        stub.assert_no_pending_responses()
