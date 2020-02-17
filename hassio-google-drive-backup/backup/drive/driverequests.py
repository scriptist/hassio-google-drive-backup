import io
import json
import math
import os
import re
from datetime import timedelta
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from aiohttp import ClientSession
from aiohttp.client import ClientTimeout
from aiohttp.client_exceptions import (ClientConnectorError,
                                       ClientResponseError, ContentTypeError,
                                       ServerTimeoutError)
from dns.exception import DNSException
from injector import inject, singleton
from yarl import URL

from ..util import AsyncHttpGetter, Resolver
from ..config import Config, Setting, VERSION
from ..exceptions import (DriveQuotaExceeded, GoogleCantConnect,
                          GoogleCredentialsExpired, GoogleDnsFailure,
                          GoogleDrivePermissionDenied, GoogleInternalError,
                          GoogleSessionError, GoogleTimeoutError, LogicError,
                          ProtocolError, ensureKey, CredRefreshGoogleError, CredRefreshMyError)
from ..time import Time
from ..logger import getLogger

logger = getLogger(__name__)

MIME_TYPE = "application/tar"
FOLDER_MIME_TYPE = 'application/vnd.google-apps.folder'
FOLDER_NAME = 'Hass.io Snapshots'
DRIVE_VERSION = "v3"
DRIVE_SERVICE = "drive"

SELECT_FIELDS = "id,name,appProperties,size,trashed,mimeType,modifiedTime,capabilities,parents"
THUMBNAIL_MIME_TYPE = "image/png"
QUERY_FIELDS = "nextPageToken,files(" + SELECT_FIELDS + ")"
CREATE_FIELDS = SELECT_FIELDS
URL_FILES = "/drive/v3/files/"
URL_UPLOAD = "/upload/drive/v3/files/?uploadType=resumable&supportsAllDrives=true"
URL_AUTH = "/oauth2/v4/token"
PAGE_SIZE = 100
CHUNK_SIZE = 5 * 262144
RANGE_RE = re.compile("^bytes=0-\\d+$")

BASE_CHUNK_SIZE = 262144  # Google's api requires uploading chunks in multiples of 256kb
# Never try to uplod mroe than 10mb at once
MAX_CHUNK_SIZE = BASE_CHUNK_SIZE * 40
# During upload, chunks get sized to complete upload after 10s so we can give status updates on progress.
CHUNK_UPLOAD_TARGET_SECONDS = 10

# don't attempt to resue a session mroe than this many times.  Just in case something is broken on Google's
# # end so we don't retry the same broken session indefinitely.
RETRY_SESSION_ATTEMPTS = 10


RATE_LIMIT_EXCEEDED = 403
TOO_MANY_REQUESTS = 429


# Defines the retry strategy for calls made to Drive
# max # of time to retry and call to Drive
DRIVE_MAX_RETRIES: int = 5
# The initial backoff for drive retries.
DRIVE_RETRY_INITIAL_SECONDS: int = 2
# How uch longer to wait for each Drive service call (Exponential backoff)
DRIVE_EXPONENTIAL_BACKOFF: int = 2


@singleton
class DriveRequests():
    @inject
    def __init__(self, config: Config, time: Time, resolver: Resolver, session: ClientSession):
        self.config = config
        self.time = time
        self.resolver = resolver

        self.cred_expiration = None
        self.cred_bearer = None
        self.cred_refresh = None
        self.cred_id = None
        self.cred_secret = None
        self.session = session
        self.tryLoadCredentials()

        # Between attempts to upload, we keep track of the info needed to resume a resumable upload.
        self.last_attempt_metadata = None
        self.last_attempt_location = None
        self.last_attempt_count = 0

    async def _getHeaders(self, refresh=False):
        return {
            "Authorization": "Bearer " + await self.getToken(refresh=refresh),
            "Client-Identifier": self.config.clientIdentifier()
        }

    def isCustomCreds(self):
        return self.config.get(Setting.DEFAULT_DRIVE_CLIENT_ID) != self.cred_id

    def _getAuthHeaders(self):
        return {
            "Client-Identifier": self.config.clientIdentifier()
        }

    def enabled(self):
        return self.cred_refresh is not None

    def _enabledCheck(self):
        if not self.enabled():
            raise LogicError(
                "Attempt to use Google Drive before credentials are configured")

    def tryLoadCredentials(self):
        if os.path.isfile(self.config.get(Setting.CREDENTIALS_FILE_PATH)):
            try:
                with open(self.config.get(Setting.CREDENTIALS_FILE_PATH)) as f:
                    loaded = json.load(f)
                self.cred_bearer = loaded['access_token']
                self.cred_refresh = loaded['refresh_token']
                self.cred_id = loaded['client_id']
                try:
                    self.cred_expiration = \
                        self.time.parse(loaded['token_expiry'])
                except Exception:
                    # just eat the error, refresh now
                    self.cred_expiration = self.time.now() - timedelta(minutes=1)
                if 'client_secret' in loaded:
                    if loaded['client_id'] == self.config.get(Setting.DEFAULT_DRIVE_CLIENT_ID):
                        # The creds are saved with the old client secret, re-save them without it
                        # so wer'e foced to use the new auth endpoint.
                        with open(self.config.get(Setting.CREDENTIALS_FILE_PATH), "w") as f:
                            loaded = json.dump({
                                'access_token': self.cred_bearer,
                                'refresh_token': self.cred_refresh,
                                'client_id': self.cred_id,
                                'token_expiry': loaded['token_expiry']
                            }, f)
                    else:
                        self.cred_secret = loaded['client_secret']
                else:
                    self.cred_secret = None
                return
            except Exception:
                pass
        self.cred_bearer = None
        self.cred_expiration = None
        self.cred_refresh = None
        self.cred_secret = None
        self.cred_id = None

    def saveCredentials(self, creds):
        with open(self.config.get(Setting.CREDENTIALS_FILE_PATH), "w") as f:
            json.dump(creds, f)
        self.tryLoadCredentials()

    async def getToken(self, refresh=False):
        if self.cred_expiration and self.time.now() + timedelta(minutes=1) < self.cred_expiration and not refresh:
            return self.cred_bearer

        # refresh the credentials
        try:
            if self.cred_secret is not None:
                data = {
                    'client_id': self.cred_id,
                    'client_secret': self.cred_secret,
                    'refresh_token': self.cred_refresh,
                    'grant_type': 'refresh_token'
                }
                logger.debug("Requesting refreshed credentials from Google Drive")
                data = await self.retryRequest("POST", URL_AUTH, is_json=True, data=data, auth_headers=self._getAuthHeaders(), cred_retry=False)
            else:
                logger.debug("Requesting refreshed credentials from backup.beechens.com")
                url = URL(self.config.get(Setting.DRIVE_REFRESH_URL)).with_query({
                    "client_id": self.cred_id,
                    'refresh_token': self.cred_refresh,
                    'client': str(self.config.clientIdentifier()),
                    'version': VERSION
                })
                async with self.session.post(str(url), headers=self._getAuthHeaders()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                    elif resp.status == 503:
                        raise CredRefreshGoogleError((await resp.json())["error"])
                    elif resp.status == 401:
                        raise GoogleCredentialsExpired()
                    else:
                        try:
                            extra = (await resp.json())["error"]
                        except BaseException:
                            extra = ""
                        raise CredRefreshMyError("HTTP {} {}".format(resp.status, extra))
        except ClientConnectorError:
            raise CredRefreshMyError("Unable to connect to https://backup.beechens.com")
        self.cred_expiration = self.time.now() + timedelta(seconds=int(ensureKey('expires_in',
                                                                                 data, "Google Drive's credential Response")))
        self.cred_bearer = \
            ensureKey('access_token', data, "Google Drive's Credential Response")
        return self.cred_bearer

    async def get(self, id):
        q = {
            "fields": SELECT_FIELDS,
            "supportsAllDrives": "true"
        }
        return await self.retryRequest("GET", URL_FILES + id + "/?" + urlencode(q), is_json=True)

    async def download(self, id, size):
        ret = AsyncHttpGetter(self.config.get(Setting.DRIVE_URL) + URL_FILES + id + "/?alt=media&supportsAllDrives=true", await self._getHeaders(), self.session, size=size)
        return ret

    async def query(self, query):
        # SOMEDAY: Add a test for page size, test server support is needed too for continuation tokens
        continuation = None
        while True:
            q = {
                "q": query,
                "fields": QUERY_FIELDS,
                "pageSize": self.config.get(Setting.GOOGLE_DRIVE_PAGE_SIZE),
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "corpora": "allDrives"
            }
            if continuation:
                q["pageToken"] = continuation
            response = await self.retryRequest("GET", URL_FILES + "?" + urlencode(q), is_json=True)
            for item in response['files']:
                yield item
            if "nextPageToken" not in response or len(response['nextPageToken']) <= 0:
                break
            else:
                continuation = response['nextPageToken']

    async def update(self, id, update_metadata):
        resp = await self.retryRequest("PATCH", URL_FILES + id + "/?supportsAllDrives=true", json=update_metadata)
        async with resp:
            pass

    async def delete(self, id):
        resp = await self.retryRequest("DELETE", URL_FILES + id + "/?supportsAllDrives=true")
        async with resp:
            pass

    async def create(self, stream, metadata, mime_type):
        # Upload logic is complicated. See https://developers.google.com/drive/api/v3/manage-uploads#resumable
        total_size = stream.size()
        location = None
        if metadata == self.last_attempt_metadata and self.last_attempt_location is not None and self.last_attempt_count < RETRY_SESSION_ATTEMPTS:
            logger.debug(
                "Attempting to resume a previosuly failed upload where we left off")
            self.last_attempt_count += 1
            # Attempt to resume from a partially completed upload.
            headers = {
                "Content-Length": "0",
                "Content-Range": "bytes */{0}".format(total_size)
            }
            initial = await self.retryRequest("PUT", self.last_attempt_location, headers=headers, patch_url=False)
            async with initial:
                if initial.status == 308:
                    # We can resume the upload, check where it left off
                    if 'Range' in initial.headers:
                        position = int(initial.headers["Range"][len("bytes=0-"):])
                        stream.position(position + 1)
                    else:
                        # No range header in the response means no bytes have been uploaded yet.
                        stream.position(0)
                    logger.debug("Resuming upload at byte {0} of {1}".format(
                        stream.position(), total_size))
                    location = self.last_attempt_location
                else:
                    logger.debug("Drive returned status code {0}, so we'll have to start the upload over again.".format(
                        initial.status))

        if location is None:
            # There is no session resume, so start a new one.
            logger.debug("Starting a new upload session with Google Drive")
            headers = {
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(total_size),
            }
            initial = await self.retryRequest("POST", URL_UPLOAD, headers=headers, json=metadata)
            async with initial:
                # Google returns a url in the header "Location", which is where subsequent requests to upload
                # the snapshot's bytes should be sent.  Logic below handles uploading the file bytes in chunks.
                location = ensureKey(
                    'Location', initial.headers, "Google Drive's Upload headers")
                self.last_attempt_count = 0
                stream.position(0)

        # Keep track of the location in case the upload fails and we want to resume where we left off.
        # "metadata" is a durable fingerprint that identifies a snapshot, so we can use it to identify a
        # resumable partial upload in future retrys.
        self.last_attempt_location = location
        self.last_attempt_metadata = metadata

        # Always start with the minimum chunk size and work up from there in case the last attempt
        # failed due to connectivity errors or ... whatever.
        current_chunk_size = BASE_CHUNK_SIZE
        while True:
            start = stream.position()
            data: io.IOBaseBytesio = await stream.read(current_chunk_size)
            chunk_size = len(data.getbuffer())
            if chunk_size == 0:
                raise LogicError(
                    "Snapshot file stream ended prematurely while uploading to Google Drive")
            headers = {
                "Content-Length": str(chunk_size),
                "Content-Range": "bytes {0}-{1}/{2}".format(start, start + chunk_size - 1, total_size)
            }
            try:
                startTime = self.time.now()
                logger.debug("Sending {0} bytes to Google Drive".format(
                    current_chunk_size))
                partial = await self.retryRequest("PUT", location, headers=headers, data=data, patch_url=False)

                # Base the next chunk size on how long it took to send the last chunk.
                current_chunk_size = self._getNextChunkSize(
                    current_chunk_size, (self.time.now() - startTime).total_seconds())
            except ClientResponseError as e:
                if math.floor(e.status / 100) == 4:
                    # clear the cached session location URI, since this usually
                    # means the endpoint is no good anymore.
                    self.last_attempt_location = None
                    self.last_attempt_metadata = None

                if e.status == 404:
                    raise GoogleSessionError()
                else:
                    raise e
            yield float(start + chunk_size) / float(total_size)
            if partial.status == 200 or partial.status == 201:
                # Upload completed, return the object json
                self.last_attempt_location = None
                self.last_attempt_metadata = None
                yield await self.get((await partial.json())['id'])
                break
            elif partial.status == 308:
                # Upload partially complete, seek to the new requested position
                range_bytes = ensureKey(
                    "Range", partial.headers, "Google Drive's upload response headers")
                if not RANGE_RE.match(range_bytes):
                    raise ProtocolError(
                        "Range", partial.headers, "Google Drive's upload response headers")
                position = int(partial.headers["Range"][len("bytes=0-"):])
                stream.position(position + 1)
            else:
                partial.raise_for_status()
            await partial.release()

    def _getNextChunkSize(self, last_chunk_size, last_chunk_seconds):
        if last_chunk_seconds <= 0:
            return MAX_CHUNK_SIZE
        next_chunk = CHUNK_UPLOAD_TARGET_SECONDS * last_chunk_size / last_chunk_seconds
        if next_chunk > MAX_CHUNK_SIZE:
            return MAX_CHUNK_SIZE
        if next_chunk < BASE_CHUNK_SIZE:
            return BASE_CHUNK_SIZE
        return math.floor(next_chunk / BASE_CHUNK_SIZE) * BASE_CHUNK_SIZE

    async def createFolder(self, metadata):
        return await self.retryRequest("POST", URL_FILES + "?supportsAllDrives=true", is_json=True, json=metadata)

    def buildTimeout(self):
        return ClientTimeout(
            sock_connect=self.config.get(
                Setting.GOOGLE_DRIVE_TIMEOUT_SECONDS),
            sock_read=self.config.get(Setting.GOOGLE_DRIVE_TIMEOUT_SECONDS))

    async def retryRequest(self, method, url, auth_headers: Optional[Dict[str, str]] = None, headers: Optional[Dict[str, str]] = None, json: Optional[Dict[str, Any]] = None, data: Any = None, is_json: bool = False, cred_retry: bool = True, patch_url: bool = True):
        backoff = DRIVE_RETRY_INITIAL_SECONDS
        attempts = 0
        refresh_token = False
        if patch_url:
            url = self.config.get(Setting.DRIVE_URL) + url
        while True:
            if auth_headers is not None:
                send_headers = auth_headers.copy()
            else:
                send_headers = await self._getHeaders(refresh=refresh_token)
                refresh_token = False
            if headers:
                send_headers.update(headers)
            attempts += 1

            logger.debug("Making Google Drive request: " + url)
            try:
                to_send = data
                if isinstance(to_send, io.BytesIO):
                    # This is a pretty low-down dirty hack, but it works and lets us reuse the byte stream.
                    # aiohttp complains if you pass it a large byte object
                    to_send = io.BytesIO(to_send.getbuffer())
                    to_send.seek(0)
                response = await self.session.request(method, url, headers=send_headers, json=json, timeout=self.buildTimeout(), data=to_send)

                if response.status < 400:
                    # Response was good, so return the deets
                    if is_json:
                        ret = await response.json()
                        await response.release()
                        return ret
                    else:
                        return response
                try:
                    await self.raiseForKnownErrors(response)
                    # Only retry 403 and 5XX error,
                    # see https://developers.google.com/drive/api/v3/manage-uploads
                    if attempts > DRIVE_MAX_RETRIES:
                        # out of retries, give up if it failed.
                        if response.status == 500 or response.status == 503:
                            raise GoogleInternalError()
                        response.raise_for_status()
                    elif response.status == 401 and cred_retry:
                        # retry with fresh creds
                        logger.debug(
                            "Google Drive credentials expired.  We'll retry with new ones.")
                        refresh_token = True
                        await self.time.sleepAsync(backoff)
                        backoff *= DRIVE_EXPONENTIAL_BACKOFF
                    elif response.status == RATE_LIMIT_EXCEEDED or response.status == TOO_MANY_REQUESTS or int(response.status / 100) == 5:
                        logger.error("Google Drive returned HTTP code: {0}: we'll retry in {1} seconds".format(
                            response.status, backoff))
                        await self.time.sleepAsync(backoff)
                        # backoff exponentially, a good practice in general but also helps resolve rate limit errors.
                        backoff *= DRIVE_EXPONENTIAL_BACKOFF
                    else:
                        response.raise_for_status()
                finally:
                    await response.release()

            except ClientConnectorError as e:
                logger.debug(
                    "Ran into trouble reaching Google Drive's servers.  We'll use alternate DNS servers on the next attempt.")
                self.resolver.toggle()
                if e.os_error.errno == -2:
                    # -2 means dns lookup failed.
                    raise GoogleDnsFailure()
                elif str(e.os_error) == "Domain name not found":
                    raise GoogleDnsFailure()
                elif e.os_error.errno in [99, 111, 10061]:
                    # 111 means connection refused
                    # Can't connect
                    raise GoogleCantConnect()
                elif "Could not contact DNS serve" in str(e.os_error):
                    # Wish there was a better way to identify this exception
                    raise GoogleDnsFailure()
                raise
            except ServerTimeoutError:
                raise GoogleTimeoutError()
            except DNSException as e:
                logger.debug(str(e))
                logger.debug(
                    "Ran into trouble resolving Google Drive's servers.  We'll use normal DNS servers on the next attempt.")
                self.resolver.toggle()
                raise GoogleDnsFailure()

    async def raiseForKnownErrors(self, response):
        try:
            message = await response.json()
        except ContentTypeError:
            return
        except ValueError:
            # parsing json failed, just give up
            return
        except TypeError:
            # Same
            return
        if "error" not in message:
            return
        error_obj = message["error"]
        if isinstance(error_obj, str):
            if error_obj == "expired":
                raise GoogleCredentialsExpired()
            else:
                raise CredRefreshGoogleError(error_obj)
        if "errors" not in error_obj:
            return
        for error in error_obj["errors"]:
            if "reason" not in error:
                continue
            if error["reason"] == "storageQuotaExceeded":
                raise DriveQuotaExceeded()
            elif error["reason"] == "forbidden":
                raise GoogleDrivePermissionDenied()
