"""Opt-in S3 listing and content-digest caching for pypiserver."""

from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
import base64
import binascii
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from tempfile import SpooledTemporaryFile
import threading
import time

from pypiserver.backend import PkgFile, guess_pkgname_and_version
from pypiserver.pkg_helpers import normalize_pkgname
from pypiserver_backend_s3.backend import S3Backend
from botocore.exceptions import ClientError


@dataclass(frozen=True)
class _Object:
    key: str
    name: str
    version: str
    identity: str
    checksum_algorithms: tuple | None = None
    checksum_type: str | None = None


class _Package(PkgFile):
    __slots__ = ("s3_object",)

    def __init__(self, obj, prefix):
        super().__init__(
            pkgname=obj.name,
            version=obj.version,
            fn=obj.key,
            relfn=obj.key.removeprefix(prefix),
        )
        self.s3_object = obj


class CachedS3Backend(S3Backend):
    """Cache listings per process and digests by S3 object identity.

    backend-set options:
      listing_ttl: seconds before detecting out-of-band writes (default 30).
      digest_cache: optional SQLite file, shared across threads/processes.

    A zero TTL disables listing reuse. Failed refreshes are never cached.
    Without a cache file, up to 4096 digests are retained in memory.
    """

    def __init__(self, config):
        super().__init__(config)
        self.listing_ttl = float(config.backend_args.get("listing_ttl", "30"))
        if not math.isfinite(self.listing_ttl) or self.listing_ttl < 0:
            raise ValueError("listing_ttl must be finite and nonnegative")
        self.cache_file = config.backend_args.get("digest_cache")
        self._listing_lock = threading.RLock()
        self._digest_locks = [threading.Lock() for _ in range(64)]
        self._memory_lock = threading.Lock()
        self._digests = OrderedDict()
        self._objects = None
        self._expires_at = 0
        self._checksum_mode_supported = True
        # Separate S3-compatible endpoints and buckets in shared cache files.
        self._scope = [self.client_args.get("endpoint_url", "aws"), self.bucket]
        if self.cache_file:
            Path(self.cache_file).parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.cache_file, timeout=30)) as db, db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS digests ("
                    "object_key TEXT, algorithm TEXT, identity TEXT, digest TEXT, "
                    "PRIMARY KEY (object_key, algorithm))"
                )

    def _identity(self, obj):
        modified = obj["LastModified"]
        return json.dumps(
            [
                obj["ETag"],
                obj.get("Size", obj.get("ContentLength")),
                modified.isoformat(),
            ]
        )

    def _invalidate(self):
        with self._listing_lock:
            self._objects = None
            self._expires_at = 0

    def _snapshot(self):
        with self._listing_lock:
            if self._objects is None or time.monotonic() >= self._expires_at:
                objects = []
                paginator = self.s3_client.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
                    for obj in page.get("Contents", []):
                        # Ignore directory markers and unrelated objects.
                        parsed = guess_pkgname_and_version(obj["Key"].rsplit("/", 1)[-1])
                        if parsed is not None:
                            objects.append(
                                _Object(
                                    obj["Key"],
                                    *parsed,
                                    self._identity(obj),
                                    tuple(obj["ChecksumAlgorithm"])
                                    if "ChecksumAlgorithm" in obj
                                    else None,
                                    obj.get("ChecksumType"),
                                )
                            )
                self._objects = tuple(objects)
                self._expires_at = time.monotonic() + self.listing_ttl
            return self._objects

    def _cache_key(self, s3_key):
        return json.dumps([*self._scope, s3_key])

    def _packages(self, objects):
        objects = tuple(objects)
        cached = self._read_digests(objects)
        # PkgFile's lazy digest fields are mutable. Never reuse them between requests.
        for obj in objects:
            pkg = _Package(obj, self.prefix)
            pkg.digest = cached.get(self._cache_key(obj.key))
            yield pkg

    def get_all_packages(self):
        return self._packages(self._snapshot())

    def find_project_packages(self, project):
        normalized = normalize_pkgname(project)
        return self._packages(
            obj for obj in self._snapshot() if normalize_pkgname(obj.name) == normalized
        )

    def package_count(self):
        return len(self._snapshot())

    def get_projects(self):
        return {normalize_pkgname(obj.name) for obj in self._snapshot()}

    def _read_digests(self, objects):
        identities = {self._cache_key(obj.key): obj.identity for obj in objects}
        if not identities:
            return {}
        if not self.cache_file:
            with self._memory_lock:
                return {
                    key: item[1]
                    for key, identity in identities.items()
                    if (item := self._digests.get((key, self.hash_algo))) and item[0] == identity
                }
        cached = {}
        # One connection per page. Chunk below SQLite's legacy parameter limit.
        with closing(sqlite3.connect(self.cache_file, timeout=30)) as db:
            keys = list(identities)
            for offset in range(0, len(keys), 900):
                chunk = keys[offset : offset + 900]
                placeholders = ",".join("?" for _ in chunk)
                rows = db.execute(
                    "SELECT object_key, identity, digest FROM digests "
                    f"WHERE algorithm=? AND object_key IN ({placeholders})",
                    (self.hash_algo, *chunk),
                )
                for key, identity, digest in rows:
                    if identity == identities[key]:
                        cached[key] = digest
        return cached

    def _read_digest(self, key, identity):
        if self.cache_file:
            with closing(sqlite3.connect(self.cache_file, timeout=30)) as db:
                row = db.execute(
                    "SELECT digest FROM digests "
                    "WHERE object_key=? AND algorithm=? AND identity=?",
                    (key, self.hash_algo, identity),
                ).fetchone()
            return row[0] if row else None
        with self._memory_lock:
            item = self._digests.get((key, self.hash_algo))
            if item and item[0] == identity:
                self._digests.move_to_end((key, self.hash_algo))
                return item[1]
        return None

    def _write_digest(self, key, identity, digest):
        self._write_digests([(key, identity, digest)])

    def _write_digests(self, entries):
        if self.cache_file:
            with closing(sqlite3.connect(self.cache_file, timeout=30)) as db, db:
                db.executemany(
                    "INSERT OR REPLACE INTO digests VALUES (?, ?, ?, ?)",
                    ((key, self.hash_algo, identity, digest) for key, identity, digest in entries),
                )
        else:
            with self._memory_lock:
                for key, identity, digest in entries:
                    self._digests[(key, self.hash_algo)] = (identity, digest)
                    self._digests.move_to_end((key, self.hash_algo))
                    if len(self._digests) > 4096:
                        self._digests.popitem(last=False)

    def _head(self, key, checksum_mode=True):
        if not self._checksum_mode_supported or not checksum_mode:
            return self.s3_client.head_object(Bucket=self.bucket, Key=key)
        try:
            return self.s3_client.head_object(
                Bucket=self.bucket,
                Key=key,
                ChecksumMode="ENABLED",
            )
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = exc.response.get("Error", {}).get("Code")
            # Older S3-compatible services may reject checksum mode; KMS may
            # allow ordinary object reads but deny checksum retrieval. A plain
            # HEAD preserves the existing metadata/download fallback in either case.
            if status not in (400, 403, 501) and code not in (
                "InvalidArgument",
                "InvalidRequest",
                "NotImplemented",
                "AccessDenied",
            ):
                raise
            if status == 501 or code == "NotImplemented":
                self._checksum_mode_supported = False
            return self.s3_client.head_object(Bucket=self.bucket, Key=key)

    def _native_digest(self, head):
        headers = head.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        # Upstream pins an older botocore which doesn't model ChecksumType.
        # Its raw response headers still expose that field safely.
        checksum_type = head.get("ChecksumType") or headers.get("x-amz-checksum-type")
        if checksum_type != "FULL_OBJECT":
            return None
        fields = {
            "sha256": ("ChecksumSHA256", "x-amz-checksum-sha256"),
            "sha1": ("ChecksumSHA1", "x-amz-checksum-sha1"),
            "sha512": ("ChecksumSHA512", "x-amz-checksum-sha512"),
            "md5": ("ChecksumMD5", "x-amz-checksum-md5"),
        }
        if self.hash_algo not in fields:
            return None
        field, header = fields[self.hash_algo]
        value = head.get(field) or headers.get(header)
        if not isinstance(value, str):
            return None
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            return None
        if len(raw) != hashlib.new(self.hash_algo).digest_size:
            return None
        return f"{self.hash_algo}={raw.hex()}"

    def digest(self, pkg):
        if self.hash_algo is None or pkg.relfn is None:
            return None
        s3_key = f"{self.prefix}{pkg.relfn}"
        key = self._cache_key(s3_key)
        # Coalesce duplicate requests without serializing unrelated artifacts.
        with self._digest_locks[hash(key) % len(self._digest_locks)]:
            obj = getattr(pkg, "s3_object", None)
            if obj:
                if pkg.digest is not None:
                    return pkg.digest
                cached = self._read_digest(key, obj.identity)
                if cached:
                    return cached
            # Listings can advertise algorithms/type without their values.
            # Avoid checksum/KMS retrieval when it cannot supply the requested
            # full-file hash. Plain HEAD still retrieves our upload metadata.
            checksum_mode = not obj or (
                obj.checksum_type != "COMPOSITE"
                and (
                    obj.checksum_algorithms is None
                    or self.hash_algo.upper() in obj.checksum_algorithms
                )
            )
            head = self._head(s3_key, checksum_mode=checksum_mode)
            identity = self._identity(head)
            cached = self._read_digest(key, identity)
            if cached:
                return cached
            native = self._native_digest(head)
            if native:
                self._write_digest(key, identity, native)
                return native
            stored = head.get("Metadata", {}).get("pypiserver-sha256")
            if self.hash_algo == "sha256" and stored:
                # Only accept well-formed metadata written by an uploader.
                if len(stored) == 64 and all(c in "0123456789abcdef" for c in stored):
                    digest = f"sha256={stored}"
                    self._write_digest(key, identity, digest)
                    return digest
            # If an overwrite races this read, S3 rejects the request instead of
            # caching new bytes under the old object's identity.
            try:
                response = self.s3_client.get_object(
                    Bucket=self.bucket,
                    Key=s3_key,
                    IfMatch=head["ETag"],
                )
                with closing(response["Body"]) as body:
                    hasher = hashlib.new(self.hash_algo)
                    for chunk in iter(lambda: body.read(1024 * 1024), b""):
                        hasher.update(chunk)
                    digest = f"{self.hash_algo}={hasher.hexdigest()}"
            except Exception:
                self._invalidate()
                raise
            self._write_digest(key, identity, digest)
            return digest

    def add_package(self, filename, stream):
        # Spool large/non-seekable uploads to disk rather than retaining them
        # in memory. Hash before uploading so SHA-256 travels with the object.
        position = stream.tell() if stream.seekable() else None
        try:
            with SpooledTemporaryFile(max_size=8 * 1024 * 1024) as staged:
                hasher = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
                    staged.write(chunk)
                staged.seek(0)
                self.s3_client.upload_fileobj(
                    Bucket=self.bucket,
                    Key=f"{self.prefix}{filename}",
                    Fileobj=staged,
                    ExtraArgs={"Metadata": {"pypiserver-sha256": hasher.hexdigest()}},
                )
        finally:
            self._invalidate()
            if position is not None:
                stream.seek(position)

    def remove_package(self, pkg):
        try:
            super().remove_package(pkg)
        finally:
            self._invalidate()
