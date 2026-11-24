# pypiserver-backend-s3

A pypiserver backend for storing packages directly to S3 or S3 like services (using boto3).

**This is for use with [`pypiserver-pluggable-backends`](https://github.com/estheruary/pypiserver-pluggable-backends).**

## Installation

```bash
pip install pypiserver-pluggable-backends pypiserver-backend-s3
```

Verify that that you can see the backend with the `pypi-server backends` command.


## Running

The basic usage is actually pretty simple.

```bash
export AWS_ACCESS_KEY=""
export AWS_SECRET_ACCESS_KEY=""

pypi-server run --backend=s3 --backend-set bucket=mybucketname prefix=pypiserver
```

You can also specify these as command line args.

```bash
pypi-server run --backend=s3 \
  --backend-set \
    bucket=mybucketname \
    prefix=pypiserver \
    access_key="" \
    secret_access_key=""
```


## No Thoughts Just Try It

```bash
aws-vault exec myprofile -- pypi-server run -P . -a . --backend=s3 --backend-set bucket=mybucket prefix=myprefix

curl http://localhost:8080/packages/

# In this repo.
python -m build
twine upload --repository-url http://localhost:8080/ dist/*

curl http://localhost:8080/packages/

pip install pypiserver-backend-s3 --extra-index-url=http://localhost:8080
```

## Running With Backblaze 

To run with Backblaze (B2) just set the `endpoint` option.

```bash
pypi-server run --backend=s3 \
  --backend-set \
    bucket=mybucketname \
    prefix=pypiserver \
    access_key="<b2_keyId>" \
    secret_access_key="<b2_appKey>"
    endpoint="https://s3.us-west-002.backblazeb2.com"
```


## Cached S3 backend (opt-in)

The original `s3` backend remains available. To reuse listings and file hashes,
select `s3-cached`:

```bash
pypi-server run --hash-algo sha256 --backend=s3-cached --backend-set \
  bucket=my-bucket prefix=packages/ default_region=us-east-1 \
  listing_ttl=30 digest_cache=./digests.sqlite3
```

Listings are cached per process for `listing_ttl` seconds (default 30; zero
disables reuse). Uploads and deletions invalidate the local listing immediately;
writes through another process or directly to S3 are discovered within the TTL.
Fresh package objects are returned for each request to avoid sharing mutable
lazy digest fields between threads.

Digests are keyed by endpoint, bucket, object key, algorithm, ETag, size, and
last-modified time. ETags are opaque identifiers and are never published as
content hashes. `digest_cache` optionally selects a local SQLite file that
survives worker restarts; without it, a bounded in-memory cache is used.
Use a local filesystem for SQLite, not a network share. Warm index pages load
hashes together over one connection and make no per-object S3 requests.

On a cache miss, one HEAD retrieves a native checksum and user metadata.
Only explicitly full-object checksums of the requested algorithm are accepted;
multipart composite checksums are not valid file hashes. Valid checksums are
converted from Base64 to the hex representation expected in package links.
Permission or service-compatibility failures for checksum mode fall back to a
plain HEAD. New uploads also store a full-file SHA-256 in
`pypiserver-sha256` object metadata. Authenticated S3 writers replacing object
contents must update or remove that metadata.

If no usable stored checksum exists, the object is streamed through the hasher
once. Conditional GET prevents caching bytes from a concurrent overwrite under
an older object's identity; a failed read invalidates the listing. Downloads
for actual installations still use the original backend.

### Optional batch checksum backfill

S3 has no synchronous multi-key HEAD API, and object listings do not contain
checksum values. An optional S3 Batch Operations Compute checksum job can
calculate **SHA256**, **FULL_OBJECT** checksums inside AWS. Download its successful
completion-report CSV files and import them into the same cache:

```bash
pypi-s3-import-checksums --bucket my-bucket --prefix packages/ \
  --cache-file ./digests.sqlite3 results.csv results-2.csv.gz
```

The importer uses one paginated listing to validate current ETags and one SQLite
transaction for the reports. It rejects stale, failed, composite, or malformed
results without downloading distributions or issuing individual HEAD requests.
It changes only the local cache. No billable AWS job is launched automatically.
Pass CSV result shards, not the job's `manifest.json`.

### Tests

```bash
python -m pip install -e '.[test]'
pytest tests
```

Tests cover native checksums, SDK transfer compatibility, cache invalidation,
concurrency, pagination, persistence, and report imports without AWS traffic.

