from contextlib import contextmanager
import hashlib
from io import BytesIO
import os
import boto3

from pypiserver.backend import Backend, PkgFile, guess_pkgname_and_version
from typing import Any, Generator, Iterable, BinaryIO, Optional

from pypiserver.config import RunConfig


def digest_file(fileobj: BinaryIO, hash_algo: str) -> str:
    """
    Reads and digests a file according to specified hashing-algorith.

    :param file_path: path to a file on disk
    :param hash_algo: any algo contained in :mod:`hashlib`
    :return: <hash_algo>=<hex_digest>

    From http://stackoverflow.com/a/21565932/548792
    """
    blocksize = 2**16
    digester = hashlib.new(hash_algo)
    for block in iter(lambda: fileobj.read(blocksize), b""):
        digester.update(block)
    return f"{hash_algo}={digester.hexdigest()}"


class S3Backend(Backend):
    def __init__(
        self,
        config: RunConfig,
    ):
        super().__init__(config)
        self.bucket = config.backend_args.get(
            "bucket", os.environ.get("PYPISERVER_BACKEND_S3_BUCKET", "")
        )
        self.prefix = config.backend_args.get(
            "prefix", os.environ.get("PYPISERVER_BACKEND_S3_PREFIX", "")
        )
        self.client_args = {}

        if endpoint := config.backend_args.get("endpoint"):
            self.client_args["endpoint_url"] = endpoint

        if access_key := config.backend_args.get("access_key"):
            self.client_args["aws_access_key_id"] = access_key

        if secret_access_key := config.backend_args.get("secret_access_key"):
            self.client_args["aws_secret_access_key"] = secret_access_key

        if default_region := config.backend_args.get("default_region"):
            self.client_args["region_name"] = default_region

        self.s3_client = boto3.client("s3", **self.client_args)

    def get_all_packages(self) -> Iterable[PkgFile]:
        paginator = self.s3_client.get_paginator("list_objects_v2")

        for batch in paginator.paginate(
            Bucket=self.bucket,
            Prefix=self.prefix,
        ):
            for obj in batch.get("Contents", []):
                name, version = guess_pkgname_and_version(obj["Key"])
                yield PkgFile(
                    pkgname=name,
                    version=version,
                    fn=obj["Key"],
                    relfn=obj["Key"].removeprefix(self.prefix),
                )

    def add_package(self, filename: str, stream: BinaryIO) -> None:
        self.s3_client.upload_fileobj(
            Bucket=self.bucket,
            Key=f"{self.prefix}{filename}",
            Fileobj=stream,
        )

    def remove_package(self, pkg: PkgFile) -> None:
        self.s3_client.delete_object(
            Bucket=self.bucket,
            Key=f"{self.prefix}{pkg.relfn}",
        )

    def exists(self, filename: str) -> bool:
        try:
            self.s3_client.head_object(Bucket=self.bucket, Key=f"{self.prefix}{filename}")
            return True
        except Exception:
            return False

    def digest(self, pkg: PkgFile) -> Optional[str]:
        if self.hash_algo is None or pkg.relfn is None:
            return None

        with self.package(pkg.relfn) as fileobj:
            return digest_file(fileobj, self.hash_algo)

    @contextmanager
    def package(self, filename: str) -> Generator[BinaryIO, Any, None]:
        buf = BytesIO()
        self.s3_client.download_fileobj(
            Bucket=self.bucket,
            Key=f"{self.prefix}{filename}",
            Fileobj=buf,
        )
        buf.seek(0)
        yield buf
        buf.close()
