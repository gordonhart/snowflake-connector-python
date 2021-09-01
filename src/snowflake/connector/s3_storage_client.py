#
# Copyright (c) 2012-2021 Snowflake Computing Inc. All right reserved.
#

from __future__ import division

import binascii
import re
import xml.etree.cElementTree as ET
from datetime import datetime
from io import IOBase
from logging import getLogger
from operator import itemgetter
from typing import TYPE_CHECKING, Any, Dict, List, NamedTuple, Optional, Tuple, Union

from cryptography.hazmat.primitives import hashes, hmac

from .compat import quote, urlparse
from .constants import (
    HTTP_HEADER_CONTENT_TYPE,
    HTTP_HEADER_VALUE_OCTET_STREAM,
    FileHeader,
    ResultStatus,
)
from .encryption_util import EncryptionMetadata
from .storage_client import SnowflakeStorageClient
from .vendored import requests

if TYPE_CHECKING:  # pragma: no cover
    from .file_transfer_agent import SnowflakeFileMeta, StorageCredential

logger = getLogger(__name__)

META_PREFIX = "x-amz-meta-"
SFC_DIGEST = "sfc-digest"

AMZ_MATDESC = "x-amz-matdesc"
AMZ_KEY = "x-amz-key"
AMZ_IV = "x-amz-iv"

ERRORNO_WSAECONNABORTED = 10053  # network connection was aborted

EXPIRED_TOKEN = "ExpiredToken"
ADDRESSING_STYLE = "virtual"  # explicit force to use virtual addressing style

RE_MULTIPLE_SPACES = re.compile(r" +")


class S3Location(NamedTuple):
    bucket_name: str
    path: str


class SnowflakeS3RestClient(SnowflakeStorageClient):
    def __init__(
        self,
        meta: "SnowflakeFileMeta",
        credentials: "StorageCredential",
        stage_info: Dict[str, Any],
        chunk_size: int,
        use_accelerate_endpoint: bool = False,
        use_s3_regional_url=False,
    ) -> None:
        """Rest client for S3 storage.

        Args:
            stage_info:
            use_accelerate_endpoint:
        """
        super().__init__(meta, stage_info, chunk_size, credentials=credentials)
        # Signature version V4
        # Addressing style Virtual Host
        self.region_name: str = stage_info["region"]
        # Multipart upload only
        self.upload_id: Optional[str] = None
        self.etags: Optional[List[str]] = None
        self.s3location: "S3Location" = (
            SnowflakeS3RestClient._extract_bucket_name_and_path(
                self.stage_info["location"]
            )
        )
        self.use_s3_regional_url = use_s3_regional_url
        # if GS sends us an endpoint, it's likely for FIPS. Use it.
        if stage_info["endPoint"]:
            self.endpoint = (
                f"https://{self.s3location.bucket_name}." + stage_info["endPoint"]
            )
        elif use_accelerate_endpoint:
            self.endpoint = (
                f"https://{self.s3location.bucket_name}.s3-accelerate.amazonaws.com"
            )
        else:
            if self.use_s3_regional_url:
                self.endpoint = f"https://{self.s3location.bucket_name}.s3.{self.region_name}.amazonaws.com"
            else:
                self.endpoint = (
                    f"https://{self.s3location.bucket_name}.s3.amazonaws.com"
                )

    @staticmethod
    def _sign_bytes(secret_key: bytes, _input: str) -> bytes:
        """Applies HMAC-SHA-256 to given string with secret_key."""
        h = hmac.HMAC(secret_key, hashes.SHA256())
        h.update(_input.encode("utf-8"))
        return h.finalize()

    @staticmethod
    def _sign_bytes_hex(secret_key: bytes, _input: str) -> bytes:
        """Convenience function, same as _sign_bytes, but returns result in hex form."""
        return binascii.hexlify(SnowflakeS3RestClient._sign_bytes(secret_key, _input))

    @staticmethod
    def _hash_bytes(_input: bytes) -> bytes:
        """Applies SHA-256 hash to given bytes."""
        digest = hashes.Hash(hashes.SHA256())
        digest.update(_input)
        return digest.finalize()

    @staticmethod
    def _hash_bytes_hex(_input: bytes) -> bytes:
        """Convenience function, same as _hash_bytes, but returns result in hex form."""
        return binascii.hexlify(SnowflakeS3RestClient._hash_bytes(_input))

    @staticmethod
    def _construct_canonicalized_and_signed_headers(
        headers: Dict[str, Union[str, List[str]]]
    ) -> Tuple[str, str]:
        """Construct canonical headers for AWS specs.

        Does not support sorting by values in case the keys are the same, don't send
        in duplicate keys!
        """
        low_key_dict = {k.lower(): v for k, v in headers.items()}
        sorted_headers = sorted(headers.keys())
        _res = [(k, low_key_dict[k]) for k in sorted_headers]
        res = []

        for k, v in _res:
            # if value is a list, convert to string delimited by comma
            if isinstance(v, list):
                v = ",".join(v)
            # if multiline header, replace withs space
            k = k.replace("\n", " ")
            res.append(k.strip() + ":" + RE_MULTIPLE_SPACES.sub(" ", v.strip()))

        ans = "\n".join(res)
        if ans:
            ans += "\n"

        return ans, ";".join(sorted_headers)

    @staticmethod
    def _construct_canonical_request_and_signed_headers(
        verb: str,
        canonical_uri_parameter: str,
        url_query: str,
        canonical_headers: Optional[Dict[str, Union[str, List[str]]]] = None,
        payload: bytes = "",
    ) -> Tuple[str, str]:
        # Build canonical query string
        # Note: this doesn't support sorting by values in case the same key is given
        #  more than once, so don't do that!
        kv = []
        if url_query:
            for e in url_query.split("&"):
                k, _, v = e.partition("=")
                kv.append((k, v))
            kv.sort(key=itemgetter(0))
        canonical_query_string = "&".join("=".join([(k), (v)]) for k, v in kv)
        (
            canonical_headers,
            signed_headers,
        ) = SnowflakeS3RestClient._construct_canonicalized_and_signed_headers(
            canonical_headers
        )

        return (
            "\n".join(
                [
                    verb,
                    canonical_uri_parameter or "/",
                    canonical_query_string,
                    canonical_headers,
                    signed_headers,
                    SnowflakeS3RestClient._hash_bytes_hex(payload).lower().decode(),
                ]
            ),
            signed_headers,
        )

    @staticmethod
    def _construct_string_to_sign(
        region_name: str,
        service_name: str,
        amzdate: str,
        canonical_request_hash: bytes,
    ) -> Tuple[str, str]:
        short_amzdate = amzdate[:8]
        scope = f"{short_amzdate}/{region_name}/{service_name}/aws4_request"
        return (
            "\n".join(
                [
                    "AWS4-HMAC-SHA256",
                    amzdate,
                    scope,
                    canonical_request_hash.decode("utf-8"),
                ]
            ),
            scope,
        )

    def _has_expired_token(self, response: requests.Response) -> bool:
        """Extract error code and error message from the S3's error response.

        Expected format:
        https://docs.aws.amazon.com/AmazonS3/latest/API/ErrorResponses.html#RESTErrorResponses

        Args:
            response: Rest error response in XML format

        Returns: True if the error response is caused by token expiration

        """
        if response.status_code != 400:
            return False
        message = response.text
        if not message or message.isspace():
            return False
        err = ET.fromstring(message)
        return err.find("Code").text == EXPIRED_TOKEN

    @staticmethod
    def _extract_bucket_name_and_path(stage_location) -> "S3Location":
        # split stage location as bucket name and path
        bucket_name, _, path = stage_location.partition("/")
        if path and not path.endswith("/"):
            path += "/"

        return S3Location(bucket_name=bucket_name, path=path)

    def _send_request_with_authentication_and_retry(
        self,
        url: str,
        verb: str,
        retry_id: Union[int, str],
        x_amz_headers: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Union[bytes, bytearray, IOBase, None] = None,
    ) -> requests.Response:
        if x_amz_headers is None:
            x_amz_headers = {}
        if headers is None:
            headers = {}
        if data is None:
            data = b""
        x_amz_headers["x-amz-content-sha256"] = self._hash_bytes_hex(data).decode(
            "utf-8"
        )
        parsed_url = urlparse(url)

        def generate_authenticated_url_and_args_v4() -> Tuple[bytes, Dict[str, bytes]]:
            t = datetime.utcnow()
            amzdate = t.strftime("%Y%m%dT%H%M%SZ")
            short_amzdate = amzdate[:8]

            x_amz_headers["x-amz-date"] = amzdate
            x_amz_headers["x-amz-security-token"] = self.credentials.creds.get("AWS_TOKEN", "")
            x_amz_headers["host"] = urlparse(url).hostname

            (
                canonical_request,
                signed_headers,
            ) = self._construct_canonical_request_and_signed_headers(
                verb=verb,
                canonical_uri_parameter=parsed_url.path
                + (f";{parsed_url.params}" if parsed_url.params else ""),
                url_query=parsed_url.query,
                canonical_headers=x_amz_headers,
                payload=data,
            )
            string_to_sign, scope = self._construct_string_to_sign(
                self.region_name,
                "s3",
                amzdate,
                self._hash_bytes_hex(canonical_request.encode("utf-8")).lower(),
            )
            kDate = self._sign_bytes(
                ("AWS4" + self.credentials.creds["AWS_SECRET_KEY"]).encode("utf-8"),
                short_amzdate,
            )
            kRegion = self._sign_bytes(kDate, self.region_name)
            kService = self._sign_bytes(kRegion, "s3")
            signing_key = self._sign_bytes(kService, "aws4_request")

            signature = self._sign_bytes_hex(signing_key, string_to_sign).lower()
            authorization_header = (
                "AWS4-HMAC-SHA256 "
                + f"Credential={self.credentials.creds['AWS_KEY_ID']}/{scope}, "
                + f"SignedHeaders={signed_headers}, "
                + f"Signature={signature.decode('utf-8')}"
            )
            headers.update(x_amz_headers)
            headers["Authorization"] = authorization_header
            rest_args = {"headers": headers}

            if data:
                rest_args["data"] = data

            return url.encode("utf-8"), rest_args

        return self._send_request_with_retry(
            verb, generate_authenticated_url_and_args_v4, retry_id
        )

    def get_file_header(self, filename: str) -> Optional[FileHeader]:
        """Gets the metadata of file in specified location.

        Args:
            filename: Name of remote file.

        Returns:
            None if HEAD returns 404, otherwise a FileHeader instance populated with metadata
        """
        path = quote(self.s3location.path + filename.lstrip("/"))
        url = self.endpoint + f"/{path}"

        retry_id = "HEAD"
        self.retry_count[retry_id] = 0
        response = self._send_request_with_authentication_and_retry(
            url=url, verb="HEAD", retry_id=retry_id
        )
        if response.status_code == 200:
            self.meta.result_status = ResultStatus.UPLOADED
            metadata = response.headers
            encryption_metadata = (
                EncryptionMetadata(
                    key=metadata.get(META_PREFIX + AMZ_KEY),
                    iv=metadata.get(META_PREFIX + AMZ_IV),
                    matdesc=metadata.get(META_PREFIX + AMZ_MATDESC),
                )
                if metadata.get(META_PREFIX + AMZ_KEY)
                else None
            )
            return FileHeader(
                digest=metadata.get(META_PREFIX + SFC_DIGEST),
                content_length=int(metadata.get("Content-Length")),
                encryption_metadata=encryption_metadata,
            )
        elif response.status_code == 404:
            logger.debug(
                f"not found. bucket: {self.s3location.bucket_name}, path: {path}"
            )
            self.meta.result_status = ResultStatus.NOT_FOUND_FILE
            return None
        else:
            response.raise_for_status()

    def _prepare_file_metadata(self) -> Dict[str, Any]:
        """Construct metadata for a file to be uploaded.

        Returns: File metadata in a dict.

        """
        s3_metadata = {
            META_PREFIX + SFC_DIGEST: self.meta.sha256_digest,
        }
        if self.encryption_metadata:
            s3_metadata.update(
                {
                    META_PREFIX + AMZ_IV: self.encryption_metadata.iv,
                    META_PREFIX + AMZ_KEY: self.encryption_metadata.key,
                    META_PREFIX + AMZ_MATDESC: self.encryption_metadata.matdesc,
                }
            )
        return s3_metadata

    def _initiate_multipart_upload(self) -> None:
        path = quote(self.s3location.path + self.meta.dst_file_name.lstrip("/"))
        url = self.endpoint + f"/{path}?uploads"
        s3_metadata = self._prepare_file_metadata()
        # initiate multipart upload
        retry_id = "Initiate"
        self.retry_count[retry_id] = 0
        response = self._send_request_with_authentication_and_retry(
            url=url,
            verb="POST",
            retry_id=retry_id,
            x_amz_headers=s3_metadata,
            headers={HTTP_HEADER_CONTENT_TYPE: HTTP_HEADER_VALUE_OCTET_STREAM},
        )
        if response.status_code == 200:
            self.upload_id = ET.fromstring(response.content)[2].text
            self.etags = [None] * self.num_of_chunks
        else:
            response.raise_for_status()

    def _upload_chunk(self, chunk_id: int, chunk: bytes) -> None:
        path = quote(self.s3location.path + self.meta.dst_file_name.lstrip("/"))
        url = self.endpoint + f"/{path}"

        if self.num_of_chunks == 1:  # single request
            s3_metadata = self._prepare_file_metadata()
            response = self._send_request_with_authentication_and_retry(
                url=url,
                verb="PUT",
                retry_id=chunk_id,
                data=chunk,
                x_amz_headers=s3_metadata,
                headers={HTTP_HEADER_CONTENT_TYPE: HTTP_HEADER_VALUE_OCTET_STREAM},
            )
            response.raise_for_status()
        else:
            # multipart PUT
            chunk_url = url + f"?partNumber={chunk_id+1}&uploadId={self.upload_id}"
            response = self._send_request_with_authentication_and_retry(
                url=chunk_url, verb="PUT", retry_id=chunk_id, data=chunk
            )
            if response.status_code == 200:
                self.etags[chunk_id] = response.headers["ETag"]
            response.raise_for_status()

    def _complete_multipart_upload(self) -> None:
        path = quote(self.s3location.path + self.meta.dst_file_name.lstrip("/"))
        url = self.endpoint + f"/{path}?uploadId={self.upload_id}"
        logger.debug("Initiating multipart upload complete")
        # Complete multipart upload
        root = ET.Element("CompleteMultipartUpload")
        for idx, etag_str in enumerate(self.etags):
            part = ET.Element("Part")
            etag = ET.Element("ETag")
            etag.text = etag_str
            part.append(etag)
            part_number = ET.Element("PartNumber")
            part_number.text = str(idx + 1)
            part.append(part_number)
            root.append(part)
        retry_id = "Complete"
        self.retry_count[retry_id] = 0
        response = self._send_request_with_authentication_and_retry(
            url=url,
            verb="POST",
            retry_id=retry_id,
            data=ET.tostring(root),
        )
        response.raise_for_status()

    def _abort_multipart_upload(self) -> None:
        if self.upload_id is None:
            return
        path = quote(self.s3location.path + self.meta.dst_file_name.lstrip("/"))
        url = self.endpoint + f"/{path}?uploadId={self.upload_id}"

        retry_id = "Abort"
        self.retry_count[retry_id] = 0
        response = self._send_request_with_authentication_and_retry(
            url=url, verb="DELETE", retry_id=retry_id
        )
        response.raise_for_status()

    def download_chunk(self, chunk_id: int) -> None:
        logger.debug(f"Downloading chunk {chunk_id}")
        path = quote(self.s3location.path + self.meta.src_file_name.lstrip("/"))
        url = self.endpoint + f"/{path}"
        if self.num_of_chunks == 1:
            response = self._send_request_with_authentication_and_retry(
                url=url, verb="GET", retry_id=chunk_id
            )
            if response.status_code == 200:
                self.write_downloaded_chunk(0, response.content)
                self.meta.result_status = ResultStatus.DOWNLOADED
            response.raise_for_status()
        else:
            chunk_size = self.chunk_size
            if chunk_id < self.num_of_chunks - 1:
                _range = f"{chunk_id * chunk_size}-{(chunk_id+1)*chunk_size-1}"
            else:
                _range = f"{chunk_id * chunk_size}-"

            response = self._send_request_with_authentication_and_retry(
                url=url,
                verb="GET",
                retry_id=chunk_id,
                headers={"Range": f"bytes={_range}"},
            )
            if response.status_code in (200, 206):
                self.write_downloaded_chunk(chunk_id, response.content)
            response.raise_for_status()

    def transfer_accelerate_config(self) -> bool:
        url = self.endpoint + "/?accelerate"
        retry_id = "accelerate"
        self.retry_count[retry_id] = 0
        response = self._send_request_with_authentication_and_retry(
            url=url, verb="GET", retry_id=retry_id
        )
        if response.status_code == 200:
            config = ET.fromstring(response.text)
            use_accelerate_endpoint = (
                config.find("Status") and config.find("Status").text == "Enabled"
            )
            logger.debug(f"use_accelerate_endpoint: {use_accelerate_endpoint}")
            return use_accelerate_endpoint
        return False
