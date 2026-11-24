"""Backend behavior tests; no AWS access required."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import base64
import csv
import hashlib
import gzip
from io import BytesIO, StringIO
import json
import sqlite3
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

from botocore.exceptions import ClientError
import pytest

from pypiserver_backend_s3.cached_backend import CachedS3Backend
from pypiserver_backend_s3.checksum_report import import_reports


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.calls = Counter()
        self.bodies = []
        self.sequence = 0

    def put(self, key, data=b"wheel bytes", metadata=None):
        self.sequence += 1
        self.objects[key] = {
            "Key": key,
            "data": data,
            "Metadata": metadata or {},
            "ETag": f'"{hashlib.md5(data).hexdigest()}"',
            "Size": len(data),
            "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc)
            + timedelta(seconds=self.sequence),
        }

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):
        self.calls["list"] += 1
        objects = [v.copy() for k, v in self.objects.items() if k.startswith(Prefix)]
        # Exercise pagination even with a small fixture.
        yield {"Contents": objects[:2]}
        yield {"Contents": objects[2:]}

    def head_object(self, Bucket, Key, ChecksumMode=None):
        self.calls["head"] += 1
        obj = self.objects[Key].copy()
        obj["ContentLength"] = obj.pop("Size")
        return obj

    def get_object(self, Bucket, Key, IfMatch):
        self.calls["get"] += 1
        obj = self.objects[Key]
        if obj["ETag"] != IfMatch:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "GetObject")
        body = BytesIO(obj["data"])
        self.bodies.append(body)
        return {"Body": body}

    def upload_fileobj(self, Bucket, Key, Fileobj, ExtraArgs):
        self.calls["upload"] += 1
        self.put(Key, Fileobj.read(), ExtraArgs["Metadata"])

    def delete_object(self, Bucket, Key):
        self.calls["delete"] += 1
        del self.objects[Key]


@pytest.fixture
def s3():
    client = FakeS3()
    client.put("packages/example-1.0-py3-none-any.whl")
    client.put("packages/example-2.0.tar.gz", b"source bytes")
    client.put("packages/other-1.0-py3-none-any.whl", b"other wheel")
    return client


@pytest.fixture
def make_backend(monkeypatch, s3):
    def make(**options):
        algorithm = options.pop("hash_algo", "sha256")
        monkeypatch.setattr("pypiserver_backend_s3.backend.boto3.client", lambda *a, **k: s3)
        return CachedS3Backend(
            SimpleNamespace(
                hash_algo=algorithm,
                backend_args={
                    "bucket": "test-bucket",
                    "prefix": "packages/",
                    **options,
                },
            )
        )

    return make


def test_repeated_index_hashes_only_once(make_backend, s3):
    backend = make_backend()
    for _ in range(2):
        packages = list(backend.find_project_packages("example"))
        assert len(packages) == 2
        for pkg in packages:
            assert backend.digest(pkg).startswith("sha256=")
    assert s3.calls == {"list": 1, "head": 2, "get": 2}
    assert all(body.closed for body in s3.bodies)


def test_listing_is_paginated_filters_invalid_keys_and_returns_fresh_packages(make_backend, s3):
    s3.put("packages/")
    s3.put("packages/notes.txt")
    s3.put("elsewhere/hidden-1.0-py3-none-any.whl")
    backend = make_backend()
    first = list(backend.get_all_packages())
    first[0].digest = "bad-value"
    second = list(backend.get_all_packages())
    assert len(first) == len(second) == 3
    assert second[0].digest is None
    assert backend.package_count() == 3
    assert backend.get_projects() == {"example", "other"}
    assert s3.calls["list"] == 1


def test_external_writes_visible_after_ttl(make_backend, s3, monkeypatch):
    clock = Mock(return_value=100)
    monkeypatch.setattr("pypiserver_backend_s3.cached_backend.time.monotonic", clock)
    backend = make_backend(listing_ttl="30")
    assert backend.package_count() == 3
    s3.put("packages/new-1.0-py3-none-any.whl")
    assert backend.package_count() == 3
    clock.return_value = 130
    assert backend.package_count() == 4


def test_disabled_listing_cache(make_backend, s3):
    backend = make_backend(listing_ttl="0")
    backend.package_count()
    backend.package_count()
    assert s3.calls["list"] == 2


@pytest.mark.parametrize("ttl", ["-1", "nan", "inf"])
def test_invalid_ttl_rejected(make_backend, ttl):
    with pytest.raises(ValueError, match="listing_ttl"):
        make_backend(listing_ttl=ttl)


def test_failed_listing_is_retried_without_partial_results(make_backend, s3, monkeypatch):
    backend = make_backend()
    original = s3.paginate

    def broken(**kwargs):
        yield next(original(**kwargs))
        raise RuntimeError("S3 unavailable")

    monkeypatch.setattr(s3, "paginate", broken)
    with pytest.raises(RuntimeError):
        backend.package_count()
    monkeypatch.setattr(s3, "paginate", original)
    assert backend.package_count() == 3


def test_persistent_hash_cache_survives_backend_restart(make_backend, s3, tmp_path):
    options = {"digest_cache": str(tmp_path / "cache" / "hashes.sqlite3")}
    backend = make_backend(**options)
    pkg = next(backend.get_all_packages())
    expected = backend.digest(pkg)
    s3.calls.clear()
    restarted = make_backend(**options)
    assert restarted.digest(next(restarted.get_all_packages())) == expected
    assert s3.calls == {"list": 1}


@pytest.mark.parametrize("changed_field", ["ETag", "Size", "LastModified"])
def test_external_overwrite_invalidates_persistent_hash(make_backend, s3, tmp_path, changed_field):
    options = {"digest_cache": str(tmp_path / "hashes.sqlite3")}
    backend = make_backend(**options)
    pkg = next(backend.get_all_packages())
    backend.digest(pkg)
    obj = s3.objects[pkg.fn]
    # Change one identity field at a time; metadata-only changes must count too.
    if changed_field == "LastModified":
        obj[changed_field] += timedelta(seconds=1)
    elif changed_field == "Size":
        obj[changed_field] += 1
    else:
        obj[changed_field] = '"replacement-etag"'
    s3.calls.clear()
    restarted = make_backend(**options)
    restarted.digest(next(restarted.get_all_packages()))
    assert s3.calls["get"] == 1


def test_cache_separates_buckets_endpoints_and_algorithms(make_backend, s3, tmp_path):
    options = {"digest_cache": str(tmp_path / "hashes.sqlite3")}
    for extra in [
        {},
        {"bucket": "another"},
        {"endpoint": "https://s3.example"},
        {"hash_algo": "md5"},
    ]:
        backend = make_backend(**options, **extra)
        backend.digest(next(backend.get_all_packages()))
    assert s3.calls["get"] == 4


def test_upload_stores_sha256_and_invalidates_listing(make_backend, s3):
    backend = make_backend()
    backend.package_count()
    stream = BytesIO(b"new wheel")
    backend.add_package("new-1.0-py3-none-any.whl", stream)
    assert not stream.closed and stream.tell() == 0
    assert backend.package_count() == 4
    pkg = next(backend.find_project_packages("new"))
    assert backend.digest(pkg) == f"sha256={hashlib.sha256(b'new wheel').hexdigest()}"
    assert s3.calls["get"] == 0
    assert s3.calls["list"] == 2


def test_overwrite_does_not_reuse_previous_hash(make_backend, s3):
    backend = make_backend()
    old = backend.digest(next(backend.get_all_packages()))
    backend.add_package("example-1.0-py3-none-any.whl", BytesIO(b"replacement"))
    new = backend.digest(next(backend.get_all_packages()))
    assert new != old
    assert new == f"sha256={hashlib.sha256(b'replacement').hexdigest()}"


def test_nonseekable_upload(make_backend, s3):
    class NonSeekable(BytesIO):
        def seekable(self):
            return False

        def seek(self, *args):
            raise AssertionError("must not seek the caller's stream")

    backend = make_backend()
    backend.add_package("new-1.0-py3-none-any.whl", NonSeekable(b"new"))
    assert s3.objects["packages/new-1.0-py3-none-any.whl"]["data"] == b"new"


def test_deletion_invalidates_listing(make_backend, s3):
    backend = make_backend()
    pkg = next(backend.get_all_packages())
    backend.remove_package(pkg)
    assert backend.package_count() == 2
    assert s3.calls["list"] == 2


def test_disabled_hashes_do_not_read_s3(make_backend, s3):
    backend = make_backend(hash_algo=None)
    assert backend.digest(next(backend.get_all_packages())) is None
    assert s3.calls == {"list": 1}


@pytest.mark.parametrize("operation", ["upload", "delete"])
def test_uncertain_write_invalidates_listing(make_backend, s3, monkeypatch, operation):
    backend = make_backend()
    pkg = next(backend.get_all_packages())

    def uncertain(**kwargs):
        # Simulate a write committed remotely before the connection failed.
        del s3.objects[pkg.fn]
        raise OSError("connection reset after write")

    method = "upload_fileobj" if operation == "upload" else "delete_object"
    monkeypatch.setattr(s3, method, uncertain)
    with pytest.raises(OSError):
        if operation == "upload":
            backend.add_package(pkg.relfn, BytesIO(b"new"))
        else:
            backend.remove_package(pkg)
    assert backend.package_count() == 2


def test_incomplete_download_is_closed_and_not_cached(make_backend, s3, monkeypatch):
    backend = make_backend()
    pkg = next(backend.get_all_packages())

    class BrokenBody(BytesIO):
        def read(self, *args):
            raise OSError("interrupted download")

    body = BrokenBody(b"partial")
    original = s3.get_object
    monkeypatch.setattr(s3, "get_object", lambda **kwargs: {"Body": body})
    with pytest.raises(OSError):
        backend.digest(pkg)
    assert body.closed
    monkeypatch.setattr(s3, "get_object", original)
    assert backend.digest(next(backend.get_all_packages())).startswith("sha256=")
    assert s3.calls["get"] == 1


def test_invalid_metadata_falls_back_to_hashing(make_backend, s3):
    s3.objects["packages/example-1.0-py3-none-any.whl"]["Metadata"] = {
        "pypiserver-sha256": "invalid"
    }
    backend = make_backend()
    assert backend.digest(next(backend.get_all_packages())).startswith("sha256=")
    assert s3.calls["get"] == 1


def test_overwrite_during_hash_is_not_cached(make_backend, s3, monkeypatch):
    backend = make_backend()
    pkg = next(backend.get_all_packages())
    original = s3.get_object

    def racing(**kwargs):
        s3.put(kwargs["Key"], b"racing overwrite")
        return original(**kwargs)

    monkeypatch.setattr(s3, "get_object", racing)
    with pytest.raises(ClientError):
        backend.digest(pkg)
    monkeypatch.setattr(s3, "get_object", original)
    assert backend.digest(next(backend.get_all_packages())) == (
        f"sha256={hashlib.sha256(b'racing overwrite').hexdigest()}"
    )
    assert s3.calls["list"] == 2


def test_concurrent_requests_coalesce_cold_reads(make_backend, s3):
    backend = make_backend()
    barrier = threading.Barrier(8)

    def request(_):
        barrier.wait(timeout=5)
        return backend.digest(next(backend.get_all_packages()))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(request, range(8)))
    assert len(set(results)) == 1
    assert s3.calls == {"list": 1, "head": 1, "get": 1}


def test_index_routes_use_cache_and_health_never_touches_s3(make_backend, s3):
    import pypiserver
    from pypiserver.backend import BackendProxy
    from wsgiref.util import setup_testing_defaults

    backend = make_backend()
    app = pypiserver.app(authenticate=[], password_file=".", hash_algo="sha256")
    app._pypiserver_config._backend = BackendProxy(backend)

    def get(path):
        env = {}
        setup_testing_defaults(env)
        env["PATH_INFO"] = path
        status = []
        response = app(env, lambda code, headers, exc_info=None: status.append(code))
        try:
            body = b"".join(response)
        finally:
            if hasattr(response, "close"):
                response.close()
        assert status == ["200 OK"]
        return body

    assert get("/health") == b"Ok"
    assert not s3.calls
    assert b"#sha256=" in get("/simple/example/")
    assert b"#sha256=" in get("/simple/example/")
    assert s3.calls == {"list": 1, "head": 2, "get": 2}


@pytest.mark.parametrize("raw_headers", [False, True])
def test_native_full_object_sha256_avoids_download(make_backend, s3, raw_headers):
    obj = next(iter(s3.objects.values()))
    expected = hashlib.sha256(obj["data"])
    checksum = base64.b64encode(expected.digest()).decode()
    obj["ChecksumSHA256"] = checksum
    if raw_headers:
        obj["ResponseMetadata"] = {"HTTPHeaders": {"x-amz-checksum-type": "FULL_OBJECT"}}
    else:
        obj["ChecksumType"] = "FULL_OBJECT"
    backend = make_backend()
    for _ in range(2):
        assert backend.digest(next(backend.get_all_packages())) == f"sha256={expected.hexdigest()}"
    assert s3.calls == {"list": 1, "head": 1}


@pytest.mark.parametrize(
    "kind,value",
    [
        ("COMPOSITE", base64.b64encode(b"x" * 32).decode() + "-2"),
        ("COMPOSITE", base64.b64encode(b"x" * 32).decode()),
        (None, base64.b64encode(b"x" * 32).decode()),
        ("FULL_OBJECT", "not-base64"),
        ("FULL_OBJECT", base64.b64encode(b"too-short").decode()),
    ],
)
def test_unusable_native_checksum_falls_back(make_backend, s3, kind, value):
    obj = next(iter(s3.objects.values()))
    obj.update(ChecksumSHA256=value, ChecksumType=kind)
    backend = make_backend()
    assert backend.digest(next(backend.get_all_packages())) == (
        f"sha256={hashlib.sha256(obj['data']).hexdigest()}"
    )
    assert s3.calls["get"] == 1


@pytest.mark.parametrize("algorithm", ["sha1", "sha512", "md5"])
def test_native_other_hashes_from_raw_headers(make_backend, s3, algorithm):
    obj = next(iter(s3.objects.values()))
    hasher = hashlib.new(algorithm, obj["data"])
    obj["ResponseMetadata"] = {
        "HTTPHeaders": {
            "x-amz-checksum-type": "FULL_OBJECT",
            f"x-amz-checksum-{algorithm}": base64.b64encode(hasher.digest()).decode(),
        }
    }
    backend = make_backend(hash_algo=algorithm)
    assert backend.digest(next(backend.get_all_packages())) == f"{algorithm}={hasher.hexdigest()}"
    assert s3.calls["get"] == 0


@pytest.mark.parametrize(
    "listing_fields",
    [
        {"ChecksumAlgorithm": ["CRC64NVME"]},
        {"ChecksumAlgorithm": ["SHA256"], "ChecksumType": "COMPOSITE"},
    ],
)
def test_listing_avoids_unusable_checksum_retrieval(make_backend, s3, monkeypatch, listing_fields):
    obj = next(iter(s3.objects.values()))
    obj.update(listing_fields)
    expected = hashlib.sha256(obj["data"]).hexdigest()
    obj["Metadata"] = {"pypiserver-sha256": expected}
    head = Mock(wraps=s3.head_object)
    monkeypatch.setattr(s3, "head_object", head)
    backend = make_backend()
    assert backend.digest(next(backend.get_all_packages())) == f"sha256={expected}"
    assert "ChecksumMode" not in head.call_args.kwargs
    assert s3.calls["get"] == 0


def test_checksum_permission_denied_retries_plain_head(make_backend, s3, monkeypatch):
    backend = make_backend()
    original = s3.head_object
    requests = []

    def head(**kwargs):
        requests.append(kwargs)
        if "ChecksumMode" in kwargs:
            raise ClientError(
                {"Error": {"Code": "403"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
                "HeadObject",
            )
        return original(**kwargs)

    monkeypatch.setattr(s3, "head_object", head)
    assert backend.digest(next(backend.get_all_packages())).startswith("sha256=")
    assert len(requests) == 2
    assert requests[0]["ChecksumMode"] == "ENABLED"
    assert "ChecksumMode" not in requests[1]


def test_unsupported_checksum_mode_is_not_retried_for_each_object(make_backend, s3, monkeypatch):
    backend = make_backend()
    original = s3.head_object
    requests = []

    def head(**kwargs):
        requests.append(kwargs)
        if "ChecksumMode" in kwargs:
            raise ClientError({"Error": {"Code": "NotImplemented"}}, "HeadObject")
        return original(**kwargs)

    monkeypatch.setattr(s3, "head_object", head)
    for pkg in backend.find_project_packages("example"):
        backend.digest(pkg)
    assert sum("ChecksumMode" in request for request in requests) == 1


def test_missing_object_is_not_retried_as_checksum_permission_error(make_backend, s3, monkeypatch):
    backend = make_backend()
    error = ClientError(
        {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
        "HeadObject",
    )
    head = Mock(side_effect=error)
    monkeypatch.setattr(s3, "head_object", head)
    with pytest.raises(ClientError):
        backend.digest(next(backend.get_all_packages()))
    assert head.call_count == 1


def test_botocore_supports_request_and_raw_checksum_type(tmp_path):
    import boto3
    from botocore.stub import Stubber

    backend = CachedS3Backend.__new__(CachedS3Backend)
    client = boto3.client(
        "s3",
        region_name="us-east-2",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    backend.s3_client = client
    backend.bucket = "test-bucket"
    backend.hash_algo = "sha256"
    backend._checksum_mode_supported = True
    expected = hashlib.sha256(b"wheel")
    with Stubber(client) as stub:
        stub.add_response(
            "head_object",
            {
                "ChecksumSHA256": base64.b64encode(expected.digest()).decode(),
                "ResponseMetadata": {"HTTPHeaders": {"x-amz-checksum-type": "FULL_OBJECT"}},
            },
            {"Bucket": "test-bucket", "Key": "test.whl", "ChecksumMode": "ENABLED"},
        )
        assert backend._native_digest(backend._head("test.whl")) == f"sha256={expected.hexdigest()}"
        stub.assert_no_pending_responses()


def test_warm_page_batches_sqlite_reads(make_backend, s3, tmp_path, monkeypatch):
    backend = make_backend(digest_cache=str(tmp_path / "hashes.sqlite3"))
    for pkg in backend.find_project_packages("example"):
        backend.digest(pkg)
    s3.calls.clear()
    connect = Mock(wraps=sqlite3.connect)
    monkeypatch.setattr("pypiserver_backend_s3.cached_backend.sqlite3.connect", connect)
    packages = list(backend.find_project_packages("example"))
    assert all(backend.digest(pkg).startswith("sha256=") for pkg in packages)
    assert connect.call_count == 1
    assert not s3.calls


def test_bulk_cache_lookup_chunks_large_indexes(make_backend, s3, tmp_path, monkeypatch):
    backend = make_backend(digest_cache=str(tmp_path / "hashes.sqlite3"))
    for i in range(1000):
        s3.put(f"packages/example-{i + 10}.0.tar.gz")
    objects = backend._snapshot()
    expected = "sha256=" + hashlib.sha256(b"wheel bytes").hexdigest()
    backend._write_digests(
        [(backend._cache_key(obj.key), obj.identity, expected) for obj in objects]
    )
    connect = Mock(wraps=sqlite3.connect)
    monkeypatch.setattr("pypiserver_backend_s3.cached_backend.sqlite3.connect", connect)
    packages = list(backend.get_all_packages())
    assert len(packages) == 1003
    assert all(pkg.digest == expected for pkg in packages)
    assert connect.call_count == 1


def checksum_report(s3, keys=None, **changes):
    report = StringIO()
    writer = csv.writer(report)
    for key in keys or s3.objects:
        obj = s3.objects[key]
        hasher = hashlib.sha256(obj["data"])
        result = {
            "checksum_base64": base64.b64encode(hasher.digest()).decode(),
            "checksum_hex": hasher.hexdigest().upper(),
            "checksumAlgorithm": "SHA256",
            "checksumType": "FULL_OBJECT",
            "etag": obj["ETag"].strip('"'),
            **changes,
        }
        writer.writerow(
            [
                "test-bucket",
                quote(key, safe=""),
                "",
                "succeeded",
                "200",
                "",
                json.dumps(result),
            ]
        )
    report.seek(0)
    return report


def test_batch_report_seeds_persistent_cache_without_head_or_download(
    make_backend, s3, tmp_path, monkeypatch
):
    backend = make_backend(digest_cache=str(tmp_path / "hashes.sqlite3"))
    report = checksum_report(s3)
    connect = Mock(wraps=sqlite3.connect)
    monkeypatch.setattr("pypiserver_backend_s3.cached_backend.sqlite3.connect", connect)
    assert import_reports(backend, [report]) == {
        "imported": 3,
        "skipped": 0,
        "stale": 0,
    }
    assert connect.call_count == 1  # One write transaction for the whole report.
    restarted = make_backend(digest_cache=backend.cache_file)
    for pkg in restarted.get_all_packages():
        assert restarted.digest(pkg).startswith("sha256=")
    assert s3.calls == {"list": 2}


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"etag": "old-object"}, "stale"),
        ({"checksumType": "COMPOSITE"}, "skipped"),
        ({"checksumAlgorithm": "CRC32"}, "skipped"),
        ({"checksum_base64": "invalid"}, "skipped"),
        ({"checksum_hex": "0" * 64}, "skipped"),
    ],
)
def test_batch_report_rejects_stale_or_unusable_checksums(make_backend, s3, changes, reason):
    backend = make_backend()
    counts = import_reports(backend, [checksum_report(s3, **changes)])
    assert counts["imported"] == 0
    assert counts[reason] == 3
    assert s3.calls == {"list": 1}


def test_batch_report_import_is_atomic_across_shards(make_backend, s3):
    backend = make_backend()
    with pytest.raises(ValueError, match="7 columns"):
        import_reports(backend, [checksum_report(s3), StringIO("invalid,row\n")])
    assert not backend._digests


def test_batch_report_decodes_keys_and_skips_wrong_bucket_or_failed_tasks(make_backend, s3):
    key = "packages/example+special-1.0.tar.gz"
    s3.put(key)
    report = checksum_report(s3, [key])
    text = report.read()
    report = StringIO(
        text + text.replace("test-bucket", "another-bucket") + text.replace("succeeded", "failed")
    )
    backend = make_backend()
    assert import_reports(backend, [report]) == {
        "imported": 1,
        "skipped": 2,
        "stale": 0,
    }
    pkg = next(backend.find_project_packages("example+special"))
    assert backend.digest(pkg).startswith("sha256=")
    assert s3.calls == {"list": 1}


@pytest.mark.parametrize("message", ["invalid JSON", "[]"])
def test_batch_report_rejects_malformed_results_atomically(make_backend, s3, message):
    backend = make_backend()
    stream = StringIO()
    csv.writer(stream).writerow(
        [
            "test-bucket",
            next(iter(s3.objects)),
            "",
            "succeeded",
            "200",
            "",
            message,
        ]
    )
    stream.seek(0)
    with pytest.raises(ValueError, match="Invalid result"):
        import_reports(backend, [checksum_report(s3), stream])
    assert not backend._digests


def test_report_cli_reads_gzip_and_uses_selected_cache(
    make_backend, s3, tmp_path, monkeypatch, capsys
):
    from pypiserver_backend_s3 import checksum_report as module

    backend = make_backend(digest_cache=str(tmp_path / "hashes.sqlite3"))
    path = tmp_path / "results.csv.gz"
    with gzip.open(path, "wt") as report:
        report.write(checksum_report(s3).read())
    constructor = Mock(return_value=backend)
    monkeypatch.setattr(module, "CachedS3Backend", constructor)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pypi-s3-import-checksums",
            "--bucket",
            "test-bucket",
            "--prefix",
            "packages",
            "--cache-file",
            backend.cache_file,
            "--region",
            "eu-west-1",
            "--endpoint",
            "https://s3.example",
            str(path),
        ],
    )
    module.main()
    assert json.loads(capsys.readouterr().out) == {
        "imported": 3,
        "skipped": 0,
        "stale": 0,
    }
    options = constructor.call_args.args[0].backend_args
    assert options["prefix"] == "packages/"
    assert options["default_region"] == "eu-west-1"
    assert options["endpoint"] == "https://s3.example"
    assert options["digest_cache"] == backend.cache_file
    assert s3.calls == {"list": 1}
