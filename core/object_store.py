"""A small S3-compatible object store client, used when ``BRIGHTSTARS_STORAGE_BACKEND=s3``.

Works against real AWS S3 or any S3-compatible provider (Cloudflare R2, Backblaze B2,
DigitalOcean Spaces, MinIO) by pointing ``BRIGHTSTARS_S3_ENDPOINT_URL`` at it. Every function
here takes a full object key (the tenant's own prefix is added by the caller, in
core/storage.py) and does one thing: no directories, no symlinks, no partial writes - just
bytes in, bytes out, one key at a time. A single PUT or GET is already atomic on every
provider this module is written for: a reader sees the object before the write or the
complete object after it, never something in between.
"""

import functools


@functools.lru_cache(maxsize=1)
def _client():
    import boto3

    from control_plane import config

    return boto3.client(
        's3',
        endpoint_url=config.s3_endpoint_url(),
        region_name=config.s3_region(),
        aws_access_key_id=config.s3_access_key_id(),
        aws_secret_access_key=config.s3_secret_access_key(),
    )


def _bucket():
    from control_plane import config
    return config.s3_bucket()


def _error_code(exc):
    return exc.response.get('Error', {}).get('Code', '')


def put_bytes(key, data, content_type=None):
    """Write ``data`` to ``key``, replacing whatever was there."""
    extra = {'ContentType': content_type} if content_type else {}
    _client().put_object(Bucket=_bucket(), Key=key, Body=data, **extra)


def put_if_absent(key, data, content_type=None):
    """Write ``data`` to ``key`` only if nothing is there yet.

    Returns True if written, False if the key already exists. Uses a conditional PUT
    (``If-None-Match: *``), which every provider this module is documented for (AWS S3,
    Cloudflare R2, Backblaze B2, DigitalOcean Spaces, MinIO) supports.
    """
    from botocore.exceptions import ClientError

    extra = {'ContentType': content_type} if content_type else {}
    try:
        _client().put_object(Bucket=_bucket(), Key=key, Body=data, IfNoneMatch='*', **extra)
        return True
    except ClientError as exc:
        if _error_code(exc) in ('PreconditionFailed', '412'):
            return False
        raise


def get_bytes(key):
    """The bytes at ``key``, or None if there is nothing there."""
    from botocore.exceptions import ClientError

    try:
        return _client().get_object(Bucket=_bucket(), Key=key)['Body'].read()
    except ClientError as exc:
        if _error_code(exc) in ('NoSuchKey', '404'):
            return None
        raise


def exists(key):
    from botocore.exceptions import ClientError

    try:
        _client().head_object(Bucket=_bucket(), Key=key)
        return True
    except ClientError as exc:
        if _error_code(exc) in ('404', 'NoSuchKey', 'NotFound'):
            return False
        raise


def delete(key):
    """Remove ``key``. Never an error if it was not there, matching every caller here,
    which already guards its own removals the same way ``os.remove`` callers do."""
    _client().delete_object(Bucket=_bucket(), Key=key)


def list_names(prefix):
    """File names directly under ``prefix`` (one level, no recursion), mirroring
    ``os.listdir()`` for a key prefix that ends in '/'."""
    paginator = _client().get_paginator('list_objects_v2')
    names = []
    for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix, Delimiter='/'):
        for obj in page.get('Contents', []):
            name = obj['Key'][len(prefix):]
            if name:
                names.append(name)
    return names


def list_all(prefix):
    """(key relative to prefix, size in bytes) for every object under ``prefix``, recursively."""
    paginator = _client().get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix):
        for obj in page.get('Contents', []):
            relative = obj['Key'][len(prefix):]
            if relative:
                yield relative, obj['Size']


def download_all(prefix, dest_dir):
    """Every object under ``prefix`` (recursively), written into ``dest_dir`` at the same
    relative path. Used only for a tenant export, where the result must be a real folder on
    disk regardless of where the files normally live."""
    import os

    paginator = _client().get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix):
        for obj in page.get('Contents', []):
            relative = obj['Key'][len(prefix):]
            if not relative:
                continue
            dest = os.path.join(dest_dir, *relative.split('/'))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            data = get_bytes(obj['Key'])
            if data is not None:
                with open(dest, 'wb') as fh:
                    fh.write(data)
