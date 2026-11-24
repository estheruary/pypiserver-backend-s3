"""Seed the digest cache from S3 Batch Operations Compute checksum reports.

This tool reads S3 metadata and writes only the local cache. It does not create
jobs, change S3 objects, or download distribution contents.
"""

import argparse
from collections import Counter
from contextlib import ExitStack
import csv
import gzip
import json
import os
from types import SimpleNamespace
from urllib.parse import unquote

from .cached_backend import CachedS3Backend


def import_reports(backend, reports):
    if backend.hash_algo != "sha256":
        raise ValueError("checksum report import requires hash_algo=sha256")
    # Validate against one current, paginated listing, rather than N HEADs.
    backend._invalidate()
    objects = {obj.key: obj for obj in backend._snapshot()}
    entries = {}
    counts = Counter()
    for report in reports:
        for line, row in enumerate(csv.reader(report), 1):
            if not row:
                continue
            if len(row) != 7:
                raise ValueError(f"Invalid completion report at row {line}: expected 7 columns")
            bucket, encoded_key, _version, status, _code, _http, message = row
            obj = objects.get(unquote(encoded_key))
            if bucket != backend.bucket or obj is None or status != "succeeded":
                counts["skipped"] += 1
                continue
            try:
                result = json.loads(message)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid result JSON at row {line}") from exc
            if not isinstance(result, dict):
                raise ValueError(f"Invalid result object at row {line}")
            # ETag is an opaque content identity, not a hash to publish. Matching
            # it prevents a report for an older object from seeding a newer one.
            etag = json.loads(obj.identity)[0].strip('"')
            report_etag = result.get("etag")
            if not isinstance(report_etag, str) or report_etag.strip('"') != etag:
                counts["stale"] += 1
                continue
            if result.get("checksumAlgorithm") != "SHA256":
                counts["skipped"] += 1
                continue
            digest = backend._native_digest(
                {
                    "ChecksumType": result.get("checksumType"),
                    "ChecksumSHA256": result.get("checksum_base64"),
                }
            )
            hex_value = result.get("checksum_hex")
            if digest is None or (
                hex_value is not None
                and (not isinstance(hex_value, str) or hex_value.lower() != digest.split("=", 1)[1])
            ):
                counts["skipped"] += 1
                continue
            entries[backend._cache_key(obj.key)] = (obj.identity, digest)
    # Validate every shard before writing anything, then use one transaction.
    backend._write_digests([(key, identity, digest) for key, (identity, digest) in entries.items()])
    return {
        "imported": len(entries),
        "skipped": counts["skipped"],
        "stale": counts["stale"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "reports",
        nargs="+",
        help="Local CSV or CSV.gz result files (not manifest.json)",
    )
    parser.add_argument(
        "--bucket",
        default=os.environ.get("S3_BUCKET"),
        required=not os.environ.get("S3_BUCKET"),
    )
    parser.add_argument("--prefix", default=os.environ.get("S3_PREFIX", ""))
    parser.add_argument(
        "--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    )
    parser.add_argument("--endpoint", help="Optional S3-compatible endpoint URL")
    parser.add_argument(
        "--cache-file",
        required=True,
        help="Local SQLite cache file used by the cached backend",
    )
    args = parser.parse_args()
    prefix = args.prefix
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    options = {
        "bucket": args.bucket,
        "prefix": prefix,
        "digest_cache": args.cache_file,
    }
    if args.region:
        options["default_region"] = args.region
    if args.endpoint:
        options["endpoint"] = args.endpoint
    backend = CachedS3Backend(SimpleNamespace(hash_algo="sha256", backend_args=options))
    with ExitStack() as stack:
        reports = [
            stack.enter_context(
                (gzip.open if path.endswith(".gz") else open)(
                    path,
                    "rt",
                    encoding="utf-8",
                    newline="",
                )
            )
            for path in args.reports
        ]
        counts = import_reports(backend, reports)
    print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
