"""Storage. ``file://`` is a directory. ``s3://bucket`` is any S3-compatible
store, including Cloudflare R2. The object layout is the same either way.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
from xml.etree import ElementTree

import urllib.request
import urllib.error


def _amz_date(now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")


def sign_v4(method: str, url: str, headers: dict, payload: bytes, access: str, secret: str,
            region: str, service: str = "s3", now: datetime | None = None) -> dict:
    """Return headers for an AWS SigV4 request, including Authorization."""
    amz_date, datestamp = _amz_date(now)
    parsed = urlparse(url)
    payload_hash = hashlib.sha256(payload).hexdigest()
    hdrs = {k.lower(): v for k, v in headers.items()}
    hdrs["host"] = parsed.netloc
    hdrs["x-amz-date"] = amz_date
    hdrs["x-amz-content-sha256"] = payload_hash
    signed_names = ";".join(sorted(hdrs))
    canonical_headers = "".join(f"{k}:{hdrs[k].strip()}\n" for k in sorted(hdrs))
    path = parsed.path or "/"
    query = parsed.query
    canonical = "\n".join([method, path, query, canonical_headers, signed_names, payload_hash])
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])

    def _h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k = _h(_h(_h(_h(("AWS4" + secret).encode(), datestamp), region), service), "aws4_request")
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed_names}, Signature={signature}"
    out = dict(headers)
    out["Host"] = parsed.netloc
    out["x-amz-date"] = amz_date
    out["x-amz-content-sha256"] = payload_hash
    out["Authorization"] = auth
    return out


class Storage:
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream", overwrite: bool = True) -> None:
        raise NotImplementedError

    def get(self, key: str) -> bytes | None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def list(self, prefix: str) -> list[str]:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        return self.get(key) is not None


class FileStorage(Storage):
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, key: str) -> str:
        key = key.lstrip("/")
        if ".." in key.split("/"):
            raise ValueError("bad key")
        path = os.path.join(self.root, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def put(self, key, data, content_type="application/octet-stream", overwrite=True):
        path = self._path(key)
        if not overwrite and os.path.exists(path):
            raise FileExistsError(key)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    def get(self, key):
        path = self._path(key)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def delete(self, key):
        path = self._path(key)
        if os.path.exists(path):
            os.remove(path)

    def list(self, prefix):
        base = self._path(prefix) if prefix.endswith("/") else self._path(prefix + "/")
        # _path creates the directory; listing a missing prefix is fine
        root = self.root
        out = []
        if not os.path.isdir(os.path.join(root, prefix.strip("/"))):
            return out
        start = os.path.join(root, prefix.strip("/"))
        for dirpath, _, files in os.walk(start):
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                out.append(rel)
        return sorted(out)


class S3Storage(Storage):
    """Path-style S3. R2 endpoint example: https://<account>.r2.cloudflarestorage.com"""

    def __init__(self, bucket: str, endpoint: str, access: str, secret: str, region: str = "auto", unsigned: bool = False):
        self.bucket = bucket
        self.endpoint = endpoint.rstrip("/")
        self.access = access
        self.secret = secret
        self.region = region
        self.unsigned = unsigned

    def _url(self, key: str) -> str:
        parts = [quote(p, safe="") for p in key.split("/")]
        return f"{self.endpoint}/{self.bucket}/{'/'.join(parts)}"

    def _request(self, method: str, key: str, data: bytes = b"", headers: dict | None = None, query: str = "") -> bytes:
        url = self._url(key) + (("?" + query) if query else "")
        hdrs = dict(headers or {})
        if not self.unsigned:
            if not self.access or not self.secret:
                raise RuntimeError("S3 storage needs NC_S3_ACCESS_KEY_ID and NC_S3_SECRET_ACCESS_KEY (or NC_S3_UNSIGNED=1 for a local mock)")
            hdrs = sign_v4(method, url, hdrs, data, self.access, self.secret, self.region)
        req = urllib.request.Request(url, data=data if method != "GET" and method != "HEAD" else None, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return b""
            if e.code == 412:
                raise FileExistsError(key) from e
            body = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"S3 {method} {key} -> {e.code} {body[:300]}") from e

    def put(self, key, data, content_type="application/octet-stream", overwrite=True):
        headers = {"Content-Type": content_type}
        if not overwrite:
            headers["If-None-Match"] = "*"
        self._request("PUT", key, data, headers)

    def get(self, key):
        try:
            data = self._request("GET", key)
        except RuntimeError as e:
            if "404" in str(e):
                return None
            raise
        return data if data else None

    def delete(self, key):
        self._request("DELETE", key)

    def list(self, prefix):
        # A small list. Continuation is followed until the mock or R2 says stop.
        out = []
        token = ""
        while True:
            q = f"list-type=2&prefix={quote(prefix, safe='')}&max-keys=1000"
            if token:
                q += "&continuation-token=" + quote(token, safe="")
            # List is on the bucket, not a key.
            url = f"{self.endpoint}/{self.bucket}?{q}"
            hdrs = {}
            if not self.unsigned:
                hdrs = sign_v4("GET", url, {}, b"", self.access, self.secret, self.region)
            req = urllib.request.Request(url, headers=hdrs, method="GET")
            with urllib.request.urlopen(req, timeout=60) as r:
                xml = r.read()
            root = ElementTree.fromstring(xml)
            ns = ""
            if root.tag.startswith("{"):
                ns = root.tag.split("}")[0] + "}"
            for el in root.findall(f"{ns}Contents/{ns}Key"):
                if el.text:
                    out.append(el.text)
            truncated = root.find(f"{ns}IsTruncated")
            next_token = root.find(f"{ns}NextContinuationToken")
            if truncated is None or truncated.text != "true" or next_token is None or not next_token.text:
                break
            token = next_token.text
        return out


def open_storage(settings) -> Storage:
    url = settings.storage
    if url.startswith("file://"):
        return FileStorage(url[len("file://"):] or settings.data_dir)
    if url.startswith("s3://"):
        bucket = urlparse(url).netloc
        endpoint = settings.s3_endpoint
        if not endpoint:
            raise RuntimeError("NC_STORAGE=s3://... needs NC_S3_ENDPOINT (for R2: https://<accountid>.r2.cloudflarestorage.com)")
        return S3Storage(bucket, endpoint, settings.s3_access_key, settings.s3_secret_key, settings.s3_region, settings.s3_unsigned)
    raise RuntimeError(f"NC_STORAGE must be file:///... or s3://bucket, not {url!r}")
