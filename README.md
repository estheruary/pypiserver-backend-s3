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

