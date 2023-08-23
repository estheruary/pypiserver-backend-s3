from contextlib import contextmanager
import hashlib
from io import BytesIO
import os
import boto3

from pypiserver.backend import Backend, PkgFile, guess_pkgname_and_version
from typing import Any, Generator, Iterable, BinaryIO, Optional

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
        config,
        s3_client=boto3.client("s3"),
        bucket_name: str = os.environ.get("PYPISERVER_BACKEND_S3_BUCKET", ""),
        bucket_key_prefix: str = os.environ.get("PYPISERVER_BACKEND_S3_PREFIX", ""),
    ):
        super().__init__(config)
        self.s3_client = s3_client
        self.bucket_name = bucket_name
        self.bucket_key_prefix = bucket_key_prefix

    def get_all_packages(self) -> Iterable[PkgFile]:
        paginator = self.s3_client.get_paginator('list_objects_v2')

        for batch in paginator.paginate(
            Bucket=self.bucket_name,
            Prefix=self.bucket_key_prefix,
        ):
            for obj in batch.get("Contents", []):
                name, version = guess_pkgname_and_version(obj["Key"])
                yield PkgFile(
                    pkgname=name,
                    version=version,
                    fn=obj["Key"],
                    relfn=obj["Key"].removeprefix(self.bucket_key_prefix),
                )

    def add_package(self, filename: str, stream: BinaryIO) -> None:
        self.s3_client.upload_fileobj(
            Bucket=self.bucket_name,
            Key=f"{self.bucket_key_prefix}{filename}",
            Fileobj=stream,
        )


    def remove_package(self, pkg: PkgFile) -> None:
        breakpoint()
        self.s3_client.delete_object(
            Bucket=self.bucket_name,
            Key=f"{self.bucket_key_prefix}{pkg.relfn}",
        )

    def exists(self, filename: str) -> bool:
        try:
            self.s3_client.head_object(
                Bucket=self.bucket_name,
                Key=f"{self.bucket_key_prefix}{filename}"
            )
            return True
        except:
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
            Bucket=self.bucket_name,
            Key=f"{self.bucket_key_prefix}{filename}",
            Fileobj=buf,
        )
        buf.seek(0)
        yield buf
        buf.close()

