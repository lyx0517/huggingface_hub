import copy
import fnmatch
import io
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Any, BinaryIO, Dict, Generator, Optional, Tuple, Union
from urllib.parse import quote, urlparse

import requests
from filelock import FileLock
from huggingface_hub import constants
from requests.exceptions import ConnectTimeout, ProxyError

from . import __version__  # noqa: F401 # for backward compatibility
from .constants import (
    DEFAULT_REVISION,
    HF_HUB_DISABLE_SYMLINKS_WARNING,
    HUGGINGFACE_CO_URL_TEMPLATE,
    HUGGINGFACE_HEADER_X_LINKED_ETAG,
    HUGGINGFACE_HEADER_X_LINKED_SIZE,
    HUGGINGFACE_HEADER_X_REPO_COMMIT,
    HUGGINGFACE_HUB_CACHE,
    REPO_ID_SEPARATOR,
    REPO_TYPES,
    REPO_TYPES_URL_PREFIXES,
)
from .utils import get_fastai_version  # noqa: F401 # for backward compatibility
from .utils import get_fastcore_version  # noqa: F401 # for backward compatibility
from .utils import get_graphviz_version  # noqa: F401 # for backward compatibility
from .utils import get_jinja_version  # noqa: F401 # for backward compatibility
from .utils import get_pydot_version  # noqa: F401 # for backward compatibility
from .utils import get_tf_version  # noqa: F401 # for backward compatibility
from .utils import get_torch_version  # noqa: F401 # for backward compatibility
from .utils import is_fastai_available  # noqa: F401 # for backward compatibility
from .utils import is_fastcore_available  # noqa: F401 # for backward compatibility
from .utils import is_graphviz_available  # noqa: F401 # for backward compatibility
from .utils import is_jinja_available  # noqa: F401 # for backward compatibility
from .utils import is_pydot_available  # noqa: F401 # for backward compatibility
from .utils import is_tf_available  # noqa: F401 # for backward compatibility
from .utils import is_torch_available  # noqa: F401 # for backward compatibility
from .utils import (
    EntryNotFoundError,
    LocalEntryNotFoundError,
    build_hf_headers,
    hf_raise_for_status,
    http_backoff,
    logging,
    tqdm,
    validate_hf_hub_args,
)
from .utils._headers import _http_user_agent
from .utils._runtime import _PY_VERSION  # noqa: F401 # for backward compatibility
from .utils._typing import HTTP_METHOD_T


logger = logging.get_logger(__name__)


_are_symlinks_supported_in_dir: Dict[str, bool] = {}


def are_symlinks_supported(cache_dir: Union[str, Path, None] = None) -> bool:
    """Return whether the symlinks are supported on the machine.

    Since symlinks support can change depending on the mounted disk, we need to check
    on the precise cache folder. By default, the default HF cache directory is checked.

    Args:
        cache_dir (`str`, `Path`, *optional*):
            Path to the folder where cached files are stored.

    Returns: [bool] Whether symlinks are supported in the directory.
    """
    # Defaults to HF cache
    if cache_dir is None:
        cache_dir = HUGGINGFACE_HUB_CACHE
    cache_dir = str(Path(cache_dir).expanduser().resolve())  # make it unique

    # Check symlink compatibility only once (per cache directory) at first time use
    if cache_dir not in _are_symlinks_supported_in_dir:
        _are_symlinks_supported_in_dir[cache_dir] = True

        os.makedirs(cache_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache_dir) as tmpdir:
            src_path = Path(tmpdir) / "dummy_file_src"
            src_path.touch()
            dst_path = Path(tmpdir) / "dummy_file_dst"

            # Relative source path as in `_create_relative_symlink``
            relative_src = os.path.relpath(src_path, start=os.path.dirname(dst_path))
            try:
                os.symlink(relative_src, dst_path)
            except OSError:
                # Likely running on Windows
                _are_symlinks_supported_in_dir[cache_dir] = False

                if not HF_HUB_DISABLE_SYMLINKS_WARNING:
                    message = (
                        "`huggingface_hub` cache-system uses symlinks by default to"
                        " efficiently store duplicated files but your machine does not"
                        f" support them in {cache_dir}. Caching files will still work"
                        " but in a degraded version that might require more space on"
                        " your disk. This warning can be disabled by setting the"
                        " `HF_HUB_DISABLE_SYMLINKS_WARNING` environment variable. For"
                        " more details, see"
                        " https://huggingface.co/docs/huggingface_hub/how-to-cache#limitations."
                    )
                    if os.name == "nt":
                        message += (
                            "\nTo support symlinks on Windows, you either need to"
                            " activate Developer Mode or to run Python as an"
                            " administrator. In order to see activate developer mode,"
                            " see this article:"
                            " https://docs.microsoft.com/en-us/windows/apps/get-started/enable-your-device-for-development"
                        )
                    warnings.warn(message)

    return _are_symlinks_supported_in_dir[cache_dir]


# Return value when trying to load a file from cache but the file does not exist in the distant repo.
_CACHED_NO_EXIST = object()
_CACHED_NO_EXIST_T = Any
REGEX_COMMIT_HASH = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class HfFileMetadata:
    """Data structure containing information about a file versioned on the Hub.

    Returned by [`get_hf_file_metadata`] based on a URL.

    Args:
        commit_hash (`str`, *optional*):
            The commit_hash related to the file.
        etag (`str`, *optional*):
            Etag of the file on the server.
        location (`str`):
            Location where to download the file. Can be a Hub url or not (CDN).
        size (`size`):
            Size of the file. In case of an LFS file, contains the size of the actual
            LFS file, not the pointer.
    """

    commit_hash: Optional[str]
    etag: Optional[str]
    location: str
    size: Optional[int]


@validate_hf_hub_args
def hf_hub_url(
    repo_id: str,
    filename: str,
    *,
    subfolder: Optional[str] = None,
    repo_type: Optional[str] = None,
    revision: Optional[str] = None,
) -> str:
    """Construct the URL of a file from the given information.

    The resolved address can either be a huggingface.co-hosted url, or a link to
    Cloudfront (a Content Delivery Network, or CDN) for large files which are
    more than a few MBs.

    Args:
        repo_id (`str`):
            A namespace (user or an organization) name and a repo name separated
            by a `/`.
        filename (`str`):
            The name of the file in the repo.
        subfolder (`str`, *optional*):
            An optional value corresponding to a folder inside the repo.
        repo_type (`str`, *optional*):
            Set to `"dataset"` or `"space"` if uploading to a dataset or space,
            `None` or `"model"` if uploading to a model. Default is `None`.
        revision (`str`, *optional*):
            An optional Git revision id which can be a branch name, a tag, or a
            commit hash.

    Example:

    ```python
    >>> from huggingface_hub import hf_hub_url

    >>> hf_hub_url(
    ...     repo_id="julien-c/EsperBERTo-small", filename="pytorch_model.bin"
    ... )
    'https://huggingface.co/julien-c/EsperBERTo-small/resolve/main/pytorch_model.bin'
    ```

    <Tip>

    Notes:

        Cloudfront is replicated over the globe so downloads are way faster for
        the end user (and it also lowers our bandwidth costs).

        Cloudfront aggressively caches files by default (default TTL is 24
        hours), however this is not an issue here because we implement a
        git-based versioning system on huggingface.co, which means that we store
        the files on S3/Cloudfront in a content-addressable way (i.e., the file
        name is its hash). Using content-addressable filenames means cache can't
        ever be stale.

        In terms of client-side caching from this library, we base our caching
        on the objects' entity tag (`ETag`), which is an identifier of a
        specific version of a resource [1]_. An object's ETag is: its git-sha1
        if stored in git, or its sha256 if stored in git-lfs.

    </Tip>

    References:

    -  [1] https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/ETag
    """
    if subfolder == "":
        subfolder = None
    if subfolder is not None:
        filename = f"{subfolder}/{filename}"

    if repo_type not in REPO_TYPES:
        raise ValueError("Invalid repo type")

    if repo_type in REPO_TYPES_URL_PREFIXES:
        repo_id = REPO_TYPES_URL_PREFIXES[repo_type] + repo_id

    if revision is None:
        revision = DEFAULT_REVISION
    return HUGGINGFACE_CO_URL_TEMPLATE.format(
        repo_id=repo_id,
        revision=quote(revision, safe=""),
        filename=quote(filename),
    )


def url_to_filename(url: str, etag: Optional[str] = None) -> str:
    """Generate a local filename from a url.

    Convert `url` into a hashed filename in a reproducible way. If `etag` is
    specified, append its hash to the url's, delimited by a period. If the url
    ends with .h5 (Keras HDF5 weights) adds '.h5' to the name so that TF 2.0 can
    identify it as a HDF5 file (see
    https://github.com/tensorflow/tensorflow/blob/00fad90125b18b80fe054de1055770cfb8fe4ba3/tensorflow/python/keras/engine/network.py#L1380)

    Args:
        url (`str`):
            The address to the file.
        etag (`str`, *optional*):
            The ETag of the file.

    Returns:
        The generated filename.
    """
    url_bytes = url.encode("utf-8")
    filename = sha256(url_bytes).hexdigest()

    if etag:
        etag_bytes = etag.encode("utf-8")
        filename += "." + sha256(etag_bytes).hexdigest()

    if url.endswith(".h5"):
        filename += ".h5"

    return filename


def filename_to_url(
    filename,
    cache_dir: Optional[str] = None,
    legacy_cache_layout: bool = False,
) -> Tuple[str, str]:
    """
    Return the url and etag (which may be `None`) stored for `filename`. Raise
    `EnvironmentError` if `filename` or its stored metadata do not exist.

    Args:
        filename (`str`):
            The name of the file
        cache_dir (`str`, *optional*):
            The cache directory to use instead of the default one.
        legacy_cache_layout (`bool`, *optional*, defaults to `False`):
            If `True`, uses the legacy file cache layout i.e. just call `hf_hub_url`
            then `cached_download`. This is deprecated as the new cache layout is
            more powerful.
    """
    if not legacy_cache_layout:
        warnings.warn(
            "`filename_to_url` uses the legacy way cache file layout",
            FutureWarning,
        )

    if cache_dir is None:
        cache_dir = HUGGINGFACE_HUB_CACHE
    if isinstance(cache_dir, Path):
        cache_dir = str(cache_dir)

    cache_path = os.path.join(cache_dir, filename)
    if not os.path.exists(cache_path):
        raise EnvironmentError(f"file {cache_path} not found")

    meta_path = cache_path + ".json"
    if not os.path.exists(meta_path):
        raise EnvironmentError(f"file {meta_path} not found")

    with open(meta_path, encoding="utf-8") as meta_file:
        metadata = json.load(meta_file)
    url = metadata["url"]
    etag = metadata["etag"]

    return url, etag


def http_user_agent(
    *,
    library_name: Optional[str] = None,
    library_version: Optional[str] = None,
    user_agent: Union[Dict, str, None] = None,
) -> str:
    """Deprecated in favor of [`build_hf_headers`]."""
    return _http_user_agent(
        library_name=library_name,
        library_version=library_version,
        user_agent=user_agent,
    )


class OfflineModeIsEnabled(ConnectionError):
    pass


def _raise_if_offline_mode_is_enabled(msg: Optional[str] = None):
    """Raise a OfflineModeIsEnabled error (subclass of ConnectionError) if
    HF_HUB_OFFLINE is True."""
    if constants.HF_HUB_OFFLINE:
        raise OfflineModeIsEnabled(
            "Offline mode is enabled."
            if msg is None
            else "Offline mode is enabled. " + str(msg)
        )


def _request_wrapper(
    method: HTTP_METHOD_T,
    url: str,
    *,
    max_retries: int = 0,
    base_wait_time: float = 0.5,
    max_wait_time: float = 2,
    timeout: float = 10.0,
    follow_relative_redirects: bool = False,
    **params,
) -> requests.Response:
    """Wrapper around requests methods to add several features.

    What it does:
    1. Ensure offline mode is disabled (env variable `HF_HUB_OFFLINE` not set to 1).
       If enabled, a `OfflineModeIsEnabled` exception is raised.
    2. Follow relative redirections if `follow_relative_redirects=True` even when
       `allow_redirection` kwarg is set to False.
    3. Retry in case request fails with a `ConnectTimeout`, with exponential backoff.

    Args:
        method (`str`):
            HTTP method, such as 'GET' or 'HEAD'.
        url (`str`):
            The URL of the resource to fetch.
        max_retries (`int`, *optional*, defaults to `0`):
            Maximum number of retries, defaults to 0 (no retries).
        base_wait_time (`float`, *optional*, defaults to `0.5`):
            Duration (in seconds) to wait before retrying the first time.
            Wait time between retries then grows exponentially, capped by
            `max_wait_time`.
        max_wait_time (`float`, *optional*, defaults to `2`):
            Maximum amount of time between two retries, in seconds.
        timeout (`float`, *optional*, defaults to `10`):
            How many seconds to wait for the server to send data before
            giving up which is passed to `requests.request`.
        follow_relative_redirects (`bool`, *optional*, defaults to `False`)
            If True, relative redirection (redirection to the same site) will be
            resolved even when `allow_redirection` kwarg is set to False. Useful when we
            want to follow a redirection to a renamed repository without following
            redirection to a CDN.
        **params (`dict`, *optional*):
            Params to pass to `requests.request`.
    """
    # 1. Check online mode
    _raise_if_offline_mode_is_enabled(f"Tried to reach {url}")

    # 2. Force relative redirection
    if follow_relative_redirects:
        response = _request_wrapper(
            method=method,
            url=url,
            max_retries=max_retries,
            base_wait_time=base_wait_time,
            max_wait_time=max_wait_time,
            timeout=timeout,
            follow_relative_redirects=False,
            **params,
        )

        # If redirection, we redirect only relative paths.
        # This is useful in case of a renamed repository.
        if 300 <= response.status_code <= 399:
            parsed_target = urlparse(response.headers["Location"])
            if parsed_target.netloc == "":
                # This means it is a relative 'location' headers, as allowed by RFC 7231.
                # (e.g. '/path/to/resource' instead of 'http://domain.tld/path/to/resource')
                # We want to follow this relative redirect !
                #
                # Highly inspired by `resolve_redirects` from requests library.
                # See https://github.com/psf/requests/blob/main/requests/sessions.py#L159
                return _request_wrapper(
                    method=method,
                    url=urlparse(url)._replace(path=parsed_target.path).geturl(),
                    max_retries=max_retries,
                    base_wait_time=base_wait_time,
                    max_wait_time=max_wait_time,
                    timeout=timeout,
                    follow_relative_redirects=True,  # resolve recursively
                    **params,
                )
        return response

    # 3. Exponential backoff
    return http_backoff(
        method=method,
        url=url,
        max_retries=max_retries,
        base_wait_time=base_wait_time,
        max_wait_time=max_wait_time,
        retry_on_exceptions=(ConnectTimeout, ProxyError),
        retry_on_status_codes=(),
        timeout=timeout,
        **params,
    )


def _request_with_retry(*args, **kwargs) -> requests.Response:
    """Deprecated method. Please use `_request_wrapper` instead.

    Alias to keep backward compatibility (used in Transformers).
    """
    return _request_wrapper(*args, **kwargs)


def http_get(
    url: str,
    temp_file: BinaryIO,
    *,
    proxies=None,
    resume_size=0,
    headers: Optional[Dict[str, str]] = None,
    timeout=10.0,
    max_retries=0,
):
    """
    Download a remote file. Do not gobble up errors, and will return errors tailored to the Hugging Face Hub.
    """
    headers = copy.deepcopy(headers) or {}
    if resume_size > 0:
        headers["Range"] = "bytes=%d-" % (resume_size,)
    r = _request_wrapper(
        method="GET",
        url=url,
        stream=True,
        proxies=proxies,
        headers=headers,
        timeout=timeout,
        max_retries=max_retries,
    )
    hf_raise_for_status(r)
    content_length = r.headers.get("Content-Length")
    total = resume_size + int(content_length) if content_length is not None else None
    progress = tqdm(
        unit="B",
        unit_scale=True,
        total=total,
        initial=resume_size,
        desc=f"Downloading (…){url[-20:]}",
        disable=bool(logger.getEffectiveLevel() == logging.NOTSET),
    )
    for chunk in r.iter_content(chunk_size=10 * 1024 * 1024):
        if chunk:  # filter out keep-alive new chunks
            progress.update(len(chunk))
            temp_file.write(chunk)
    progress.close()


@validate_hf_hub_args
def cached_download(
    url: str,
    *,
    library_name: Optional[str] = None,
    library_version: Optional[str] = None,
    cache_dir: Union[str, Path, None] = None,
    user_agent: Union[Dict, str, None] = None,
    force_download: bool = False,
    force_filename: Optional[str] = None,
    proxies: Optional[Dict] = None,
    etag_timeout: float = 10,
    resume_download: bool = False,
    token: Union[bool, str, None] = None,
    local_files_only: bool = False,
    legacy_cache_layout: bool = False,
) -> Optional[str]:  # pragma: no cover
    """
    Download from a given URL and cache it if it's not already present in the
    local cache.

    Given a URL, this function looks for the corresponding file in the local
    cache. If it's not there, download it. Then return the path to the cached
    file.

    Will raise errors tailored to the Hugging Face Hub.

    Args:
        url (`str`):
            The path to the file to be downloaded.
        library_name (`str`, *optional*):
            The name of the library to which the object corresponds.
        library_version (`str`, *optional*):
            The version of the library.
        cache_dir (`str`, `Path`, *optional*):
            Path to the folder where cached files are stored.
        user_agent (`dict`, `str`, *optional*):
            The user-agent info in the form of a dictionary or a string.
        force_download (`bool`, *optional*, defaults to `False`):
            Whether the file should be downloaded even if it already exists in
            the local cache.
        force_filename (`str`, *optional*):
            Use this name instead of a generated file name.
        proxies (`dict`, *optional*):
            Dictionary mapping protocol to the URL of the proxy passed to
            `requests.request`.
        etag_timeout (`float`, *optional* defaults to `10`):
            When fetching ETag, how many seconds to wait for the server to send
            data before giving up which is passed to `requests.request`.
        resume_download (`bool`, *optional*, defaults to `False`):
            If `True`, resume a previously interrupted download.
        token (`bool`, `str`, *optional*):
            A token to be used for the download.
                - If `True`, the token is read from the HuggingFace config
                  folder.
                - If a string, it's used as the authentication token.
        local_files_only (`bool`, *optional*, defaults to `False`):
            If `True`, avoid downloading the file and return the path to the
            local cached file if it exists.
        legacy_cache_layout (`bool`, *optional*, defaults to `False`):
            Set this parameter to `True` to mention that you'd like to continue
            the old cache layout. Putting this to `True` manually will not raise
            any warning when using `cached_download`. We recommend using
            `hf_hub_download` to take advantage of the new cache.

    Returns:
        Local path (string) of file or if networking is off, last version of
        file cached on disk.

    <Tip>

    Raises the following errors:

        - [`EnvironmentError`](https://docs.python.org/3/library/exceptions.html#EnvironmentError)
          if `token=True` and the token cannot be found.
        - [`OSError`](https://docs.python.org/3/library/exceptions.html#OSError)
          if ETag cannot be determined.
        - [`ValueError`](https://docs.python.org/3/library/exceptions.html#ValueError)
          if some parameter value is invalid
        - [`~utils.RepositoryNotFoundError`]
          If the repository to download from cannot be found. This may be because it doesn't exist,
          or because it is set to `private` and you do not have access.
        - [`~utils.RevisionNotFoundError`]
          If the revision to download from cannot be found.
        - [`~utils.EntryNotFoundError`]
          If the file to download cannot be found.
        - [`~utils.LocalEntryNotFoundError`]
          If network is disabled or unavailable and file is not found in cache.

    </Tip>
    """
    if not legacy_cache_layout:
        warnings.warn(
            "`cached_download` is the legacy way to download files from the HF hub,"
            " please consider upgrading to `hf_hub_download`",
            FutureWarning,
        )

    if cache_dir is None:
        cache_dir = HUGGINGFACE_HUB_CACHE
    if isinstance(cache_dir, Path):
        cache_dir = str(cache_dir)

    os.makedirs(cache_dir, exist_ok=True)

    headers = build_hf_headers(
        token=token,
        library_name=library_name,
        library_version=library_version,
        user_agent=user_agent,
    )

    url_to_download = url
    etag = None
    if not local_files_only:
        try:
            r = _request_wrapper(
                method="HEAD",
                url=url,
                headers=headers,
                allow_redirects=False,
                follow_relative_redirects=True,
                proxies=proxies,
                timeout=etag_timeout,
            )
            hf_raise_for_status(r)
            etag = r.headers.get("X-Linked-Etag") or r.headers.get("ETag")
            # We favor a custom header indicating the etag of the linked resource, and
            # we fallback to the regular etag header.
            # If we don't have any of those, raise an error.
            if etag is None:
                raise OSError(
                    "Distant resource does not have an ETag, we won't be able to"
                    " reliably ensure reproducibility."
                )
            # In case of a redirect, save an extra redirect on the request.get call,
            # and ensure we download the exact atomic version even if it changed
            # between the HEAD and the GET (unlikely, but hey).
            # Useful for lfs blobs that are stored on a CDN.
            if 300 <= r.status_code <= 399:
                url_to_download = r.headers["Location"]
        except (requests.exceptions.SSLError, requests.exceptions.ProxyError):
            # Actually raise for those subclasses of ConnectionError
            raise
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            OfflineModeIsEnabled,
        ):
            # Otherwise, our Internet connection is down.
            # etag is None
            pass

    filename = (
        force_filename if force_filename is not None else url_to_filename(url, etag)
    )

    # get cache path to put the file
    cache_path = os.path.join(cache_dir, filename)

    # etag is None == we don't have a connection or we passed local_files_only.
    # try to get the last downloaded one
    if etag is None:
        if os.path.exists(cache_path) and not force_download:
            return cache_path
        else:
            matching_files = [
                file
                for file in fnmatch.filter(
                    os.listdir(cache_dir), filename.split(".")[0] + ".*"
                )
                if not file.endswith(".json") and not file.endswith(".lock")
            ]
            if (
                len(matching_files) > 0
                and not force_download
                and force_filename is None
            ):
                return os.path.join(cache_dir, matching_files[-1])
            else:
                # If files cannot be found and local_files_only=True,
                # the models might've been found if local_files_only=False
                # Notify the user about that
                if local_files_only:
                    raise LocalEntryNotFoundError(
                        "Cannot find the requested files in the cached path and"
                        " outgoing traffic has been disabled. To enable model look-ups"
                        " and downloads online, set 'local_files_only' to False."
                    )
                else:
                    raise LocalEntryNotFoundError(
                        "Connection error, and we cannot find the requested files in"
                        " the cached path. Please try again or make sure your Internet"
                        " connection is on."
                    )

    # From now on, etag is not None.
    if os.path.exists(cache_path) and not force_download:
        return cache_path

    # Prevent parallel downloads of the same file with a lock.
    lock_path = cache_path + ".lock"

    # Some Windows versions do not allow for paths longer than 255 characters.
    # In this case, we must specify it is an extended path by using the "\\?\" prefix.
    if os.name == "nt" and len(os.path.abspath(lock_path)) > 255:
        lock_path = "\\\\?\\" + os.path.abspath(lock_path)

    if os.name == "nt" and len(os.path.abspath(cache_path)) > 255:
        cache_path = "\\\\?\\" + os.path.abspath(cache_path)

    with FileLock(lock_path):
        # If the download just completed while the lock was activated.
        if os.path.exists(cache_path) and not force_download:
            # Even if returning early like here, the lock will be released.
            return cache_path

        if resume_download:
            incomplete_path = cache_path + ".incomplete"

            @contextmanager
            def _resumable_file_manager() -> Generator[io.BufferedWriter, None, None]:
                with open(incomplete_path, "ab") as f:
                    yield f

            temp_file_manager = _resumable_file_manager
            if os.path.exists(incomplete_path):
                resume_size = os.stat(incomplete_path).st_size
            else:
                resume_size = 0
        else:
            temp_file_manager = partial(  # type: ignore
                tempfile.NamedTemporaryFile, mode="wb", dir=cache_dir, delete=False
            )
            resume_size = 0

        # Download to temporary file, then copy to cache dir once finished.
        # Otherwise you get corrupt cache entries if the download gets interrupted.
        with temp_file_manager() as temp_file:
            logger.info("downloading %s to %s", url, temp_file.name)

            http_get(
                url_to_download,
                temp_file,
                proxies=proxies,
                resume_size=resume_size,
                headers=headers,
            )

        logger.info("storing %s in cache at %s", url, cache_path)
        _chmod_and_replace(temp_file.name, cache_path)

        if force_filename is None:
            logger.info("creating metadata file for %s", cache_path)
            meta = {"url": url, "etag": etag}
            meta_path = cache_path + ".json"
            with open(meta_path, "w") as meta_file:
                json.dump(meta, meta_file)

    return cache_path


def _normalize_etag(etag: Optional[str]) -> Optional[str]:
    """Normalize ETag HTTP header, so it can be used to create nice filepaths.

    The HTTP spec allows two forms of ETag:
      ETag: W/"<etag_value>"
      ETag: "<etag_value>"

    The hf.co hub guarantees to only send the second form.

    Args:
        etag (`str`, *optional*): HTTP header

    Returns:
        `str` or `None`: string that can be used as a nice directory name.
        Returns `None` if input is None.
    """
    if etag is None:
        return None
    return etag.strip('"')


def _create_relative_symlink(src: str, dst: str, new_blob: bool = False) -> None:
    """Create a symbolic link named dst pointing to src as a relative path to dst.

    The relative part is mostly because it seems more elegant to the author.

    The result layout looks something like
        └── [ 128]  snapshots
            ├── [ 128]  2439f60ef33a0d46d85da5001d52aeda5b00ce9f
            │   ├── [  52]  README.md -> ../../blobs/d7edf6bd2a681fb0175f7735299831ee1b22b812
            │   └── [  76]  pytorch_model.bin -> ../../blobs/403450e234d65943a7dcf7e05a771ce3c92faa84dd07db4ac20f592037a1e4bd

    If symlinks cannot be created on this platform (most likely to be Windows), the
    workaround is to avoid symlinks by having the actual file in `dst`. If it is a new
    file (`new_blob=True`), we move it to `dst`. If it is not a new file
    (`new_blob=False`), we don't know if the blob file is already referenced elsewhere.
    To avoid breaking existing cache, the file is duplicated on the disk.

    In case symlinks are not supported, a warning message is displayed to the user once
    when loading `huggingface_hub`. The warning message can be disable with the
    `DISABLE_SYMLINKS_WARNING` environment variable.
    """
    try:
        os.remove(dst)
    except OSError:
        pass

    cache_dir = os.path.dirname(os.path.commonpath([src, dst]))
    if are_symlinks_supported(cache_dir=cache_dir):
        relative_src = os.path.relpath(src, start=os.path.dirname(dst))
        try:
            os.symlink(relative_src, dst)
        except FileExistsError:
            if os.path.islink(dst) and os.path.realpath(dst) == os.path.realpath(src):
                # `dst` already exists and is a symlink to the `src` blob. It is most
                # likely that the file has been cached twice concurrently (exactly
                # between `os.remove` and `os.symlink`). Do nothing.
                pass
            else:
                # Very unlikely to happen. Means a file `dst` has been created exactly
                # between `os.remove` and `os.symlink` and is not a symlink to the `src`
                # blob file. Raise exception.
                raise
    elif new_blob:
        os.replace(src, dst)
    else:
        shutil.copyfile(src, dst)


def _cache_commit_hash_for_specific_revision(
    storage_folder: str, revision: str, commit_hash: str
) -> None:
    """Cache reference between a revision (tag, branch or truncated commit hash) and the corresponding commit hash.

    Does nothing if `revision` is already a proper `commit_hash` or reference is already cached.
    """
    if revision != commit_hash:
        ref_path = Path(storage_folder) / "refs" / revision
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        if not ref_path.exists() or commit_hash != ref_path.read_text():
            # Update ref only if has been updated. Could cause useless error in case
            # repo is already cached and user doesn't have write access to cache folder.
            # See https://github.com/huggingface/huggingface_hub/issues/1216.
            ref_path.write_text(commit_hash)


@validate_hf_hub_args
def repo_folder_name(*, repo_id: str, repo_type: str) -> str:
    """Return a serialized version of a hf.co repo name and type, safe for disk storage
    as a single non-nested folder.

    Example: models--julien-c--EsperBERTo-small
    """
    # remove all `/` occurrences to correctly convert repo to directory name
    parts = [f"{repo_type}s", *repo_id.split("/")]
    return REPO_ID_SEPARATOR.join(parts)


@validate_hf_hub_args
def hf_hub_download(
    repo_id: str,
    filename: str,
    *,
    subfolder: Optional[str] = None,
    repo_type: Optional[str] = None,
    revision: Optional[str] = None,
    library_name: Optional[str] = None,
    library_version: Optional[str] = None,
    cache_dir: Union[str, Path, None] = None,
    user_agent: Union[Dict, str, None] = None,
    force_download: bool = False,
    force_filename: Optional[str] = None,
    proxies: Optional[Dict] = None,
    etag_timeout: float = 10,
    resume_download: bool = False,
    token: Union[bool, str, None] = None,
    local_files_only: bool = False,
    legacy_cache_layout: bool = False,
):
    """Download a given file if it's not already present in the local cache.

    The new cache file layout looks like this:
    - The cache directory contains one subfolder per repo_id (namespaced by repo type)
    - inside each repo folder:
        - refs is a list of the latest known revision => commit_hash pairs
        - blobs contains the actual file blobs (identified by their git-sha or sha256, depending on
          whether they're LFS files or not)
        - snapshots contains one subfolder per commit, each "commit" contains the subset of the files
          that have been resolved at that particular commit. Each filename is a symlink to the blob
          at that particular commit.

    ```
    [  96]  .
    └── [ 160]  models--julien-c--EsperBERTo-small
        ├── [ 160]  blobs
        │   ├── [321M]  403450e234d65943a7dcf7e05a771ce3c92faa84dd07db4ac20f592037a1e4bd
        │   ├── [ 398]  7cb18dc9bafbfcf74629a4b760af1b160957a83e
        │   └── [1.4K]  d7edf6bd2a681fb0175f7735299831ee1b22b812
        ├── [  96]  refs
        │   └── [  40]  main
        └── [ 128]  snapshots
            ├── [ 128]  2439f60ef33a0d46d85da5001d52aeda5b00ce9f
            │   ├── [  52]  README.md -> ../../blobs/d7edf6bd2a681fb0175f7735299831ee1b22b812
            │   └── [  76]  pytorch_model.bin -> ../../blobs/403450e234d65943a7dcf7e05a771ce3c92faa84dd07db4ac20f592037a1e4bd
            └── [ 128]  bbc77c8132af1cc5cf678da3f1ddf2de43606d48
                ├── [  52]  README.md -> ../../blobs/7cb18dc9bafbfcf74629a4b760af1b160957a83e
                └── [  76]  pytorch_model.bin -> ../../blobs/403450e234d65943a7dcf7e05a771ce3c92faa84dd07db4ac20f592037a1e4bd
    ```

    Args:
        repo_id (`str`):
            A user or an organization name and a repo name separated by a `/`.
        filename (`str`):
            The name of the file in the repo.
        subfolder (`str`, *optional*):
            An optional value corresponding to a folder inside the model repo.
        repo_type (`str`, *optional*):
            Set to `"dataset"` or `"space"` if uploading to a dataset or space,
            `None` or `"model"` if uploading to a model. Default is `None`.
        revision (`str`, *optional*):
            An optional Git revision id which can be a branch name, a tag, or a
            commit hash.
        library_name (`str`, *optional*):
            The name of the library to which the object corresponds.
        library_version (`str`, *optional*):
            The version of the library.
        cache_dir (`str`, `Path`, *optional*):
            Path to the folder where cached files are stored.
        user_agent (`dict`, `str`, *optional*):
            The user-agent info in the form of a dictionary or a string.
        force_download (`bool`, *optional*, defaults to `False`):
            Whether the file should be downloaded even if it already exists in
            the local cache.
        proxies (`dict`, *optional*):
            Dictionary mapping protocol to the URL of the proxy passed to
            `requests.request`.
        etag_timeout (`float`, *optional*, defaults to `10`):
            When fetching ETag, how many seconds to wait for the server to send
            data before giving up which is passed to `requests.request`.
        resume_download (`bool`, *optional*, defaults to `False`):
            If `True`, resume a previously interrupted download.
        token (`str`, `bool`, *optional*):
            A token to be used for the download.
                - If `True`, the token is read from the HuggingFace config
                  folder.
                - If a string, it's used as the authentication token.
        local_files_only (`bool`, *optional*, defaults to `False`):
            If `True`, avoid downloading the file and return the path to the
            local cached file if it exists.
        legacy_cache_layout (`bool`, *optional*, defaults to `False`):
            If `True`, uses the legacy file cache layout i.e. just call [`hf_hub_url`]
            then `cached_download`. This is deprecated as the new cache layout is
            more powerful.

    Returns:
        Local path (string) of file or if networking is off, last version of
        file cached on disk.

    <Tip>

    Raises the following errors:

        - [`EnvironmentError`](https://docs.python.org/3/library/exceptions.html#EnvironmentError)
          if `token=True` and the token cannot be found.
        - [`OSError`](https://docs.python.org/3/library/exceptions.html#OSError)
          if ETag cannot be determined.
        - [`ValueError`](https://docs.python.org/3/library/exceptions.html#ValueError)
          if some parameter value is invalid
        - [`~utils.RepositoryNotFoundError`]
          If the repository to download from cannot be found. This may be because it doesn't exist,
          or because it is set to `private` and you do not have access.
        - [`~utils.RevisionNotFoundError`]
          If the revision to download from cannot be found.
        - [`~utils.EntryNotFoundError`]
          If the file to download cannot be found.
        - [`~utils.LocalEntryNotFoundError`]
          If network is disabled or unavailable and file is not found in cache.

    </Tip>
    """
    if force_filename is not None:
        warnings.warn(
            "The `force_filename` parameter is deprecated as a new caching system, "
            "which keeps the filenames as they are on the Hub, is now in place.",
            FutureWarning,
        )
        legacy_cache_layout = True

    if legacy_cache_layout:
        url = hf_hub_url(
            repo_id,
            filename,
            subfolder=subfolder,
            repo_type=repo_type,
            revision=revision,
        )

        return cached_download(
            url,
            library_name=library_name,
            library_version=library_version,
            cache_dir=cache_dir,
            user_agent=user_agent,
            force_download=force_download,
            force_filename=force_filename,
            proxies=proxies,
            etag_timeout=etag_timeout,
            resume_download=resume_download,
            token=token,
            local_files_only=local_files_only,
            legacy_cache_layout=legacy_cache_layout,
        )

    if cache_dir is None:
        cache_dir = HUGGINGFACE_HUB_CACHE
    if revision is None:
        revision = DEFAULT_REVISION
    if isinstance(cache_dir, Path):
        cache_dir = str(cache_dir)

    if subfolder == "":
        subfolder = None
    if subfolder is not None:
        # This is used to create a URL, and not a local path, hence the forward slash.
        filename = f"{subfolder}/{filename}"

    if repo_type is None:
        repo_type = "model"
    if repo_type not in REPO_TYPES:
        raise ValueError(
            f"Invalid repo type: {repo_type}. Accepted repo types are:"
            f" {str(REPO_TYPES)}"
        )

    storage_folder = os.path.join(
        cache_dir, repo_folder_name(repo_id=repo_id, repo_type=repo_type)
    )
    os.makedirs(storage_folder, exist_ok=True)

    # cross platform transcription of filename, to be used as a local file path.
    relative_filename = os.path.join(*filename.split("/"))

    # if user provides a commit_hash and they already have the file on disk,
    # shortcut everything.
    if REGEX_COMMIT_HASH.match(revision):
        pointer_path = os.path.join(
            storage_folder, "snapshots", revision, relative_filename
        )
        if os.path.exists(pointer_path):
            return pointer_path

    url = hf_hub_url(repo_id, filename, repo_type=repo_type, revision=revision)

    headers = build_hf_headers(
        token=token,
        library_name=library_name,
        library_version=library_version,
        user_agent=user_agent,
    )

    url_to_download = url
    etag = None
    commit_hash = None
    if not local_files_only:
        try:
            try:
                metadata = get_hf_file_metadata(
                    url=url,
                    token=token,
                    proxies=proxies,
                    timeout=etag_timeout,
                )
            except EntryNotFoundError as http_error:
                # Cache the non-existence of the file and raise
                commit_hash = http_error.response.headers.get(
                    HUGGINGFACE_HEADER_X_REPO_COMMIT
                )
                if commit_hash is not None and not legacy_cache_layout:
                    no_exist_file_path = (
                        Path(storage_folder)
                        / ".no_exist"
                        / commit_hash
                        / relative_filename
                    )
                    no_exist_file_path.parent.mkdir(parents=True, exist_ok=True)
                    no_exist_file_path.touch()
                    _cache_commit_hash_for_specific_revision(
                        storage_folder, revision, commit_hash
                    )
                raise

            # Commit hash must exist
            commit_hash = metadata.commit_hash
            if commit_hash is None:
                raise OSError(
                    "Distant resource does not seem to be on huggingface.co (missing"
                    " commit header)."
                )

            # Etag must exist
            etag = metadata.etag
            # We favor a custom header indicating the etag of the linked resource, and
            # we fallback to the regular etag header.
            # If we don't have any of those, raise an error.
            if etag is None:
                raise OSError(
                    "Distant resource does not have an ETag, we won't be able to"
                    " reliably ensure reproducibility."
                )

            # In case of a redirect, save an extra redirect on the request.get call,
            # and ensure we download the exact atomic version even if it changed
            # between the HEAD and the GET (unlikely, but hey).
            # Useful for lfs blobs that are stored on a CDN.
            if metadata.location != url:
                url_to_download = metadata.location
                if (
                    "lfs.huggingface.co" in url_to_download
                    or "lfs-staging.huggingface.co" in url_to_download
                ):
                    # Remove authorization header when downloading a LFS blob
                    headers.pop("authorization", None)
        except (requests.exceptions.SSLError, requests.exceptions.ProxyError):
            # Actually raise for those subclasses of ConnectionError
            raise
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            OfflineModeIsEnabled,
        ):
            # Otherwise, our Internet connection is down.
            # etag is None
            pass

    # etag is None == we don't have a connection or we passed local_files_only.
    # try to get the last downloaded one from the specified revision.
    # If the specified revision is a commit hash, look inside "snapshots".
    # If the specified revision is a branch or tag, look inside "refs".
    if etag is None:
        # In those cases, we cannot force download.
        if force_download:
            raise ValueError(
                "We have no connection or you passed local_files_only, so"
                " force_download is not an accepted option."
            )
        if REGEX_COMMIT_HASH.match(revision):
            commit_hash = revision
        else:
            ref_path = os.path.join(storage_folder, "refs", revision)
            with open(ref_path) as f:
                commit_hash = f.read()

        pointer_path = os.path.join(
            storage_folder, "snapshots", commit_hash, relative_filename
        )
        if os.path.exists(pointer_path):
            return pointer_path

        # If we couldn't find an appropriate file on disk,
        # raise an error.
        # If files cannot be found and local_files_only=True,
        # the models might've been found if local_files_only=False
        # Notify the user about that
        if local_files_only:
            raise LocalEntryNotFoundError(
                "Cannot find the requested files in the disk cache and"
                " outgoing traffic has been disabled. To enable hf.co look-ups"
                " and downloads online, set 'local_files_only' to False."
            )
        else:
            raise LocalEntryNotFoundError(
                "Connection error, and we cannot find the requested files in"
                " the disk cache. Please try again or make sure your Internet"
                " connection is on."
            )

    # From now on, etag and commit_hash are not None.
    assert etag is not None, "etag must have been retrieved from server"
    assert commit_hash is not None, "commit_hash must have been retrieved from server"
    blob_path = os.path.join(storage_folder, "blobs", etag)
    pointer_path = os.path.join(
        storage_folder, "snapshots", commit_hash, relative_filename
    )

    os.makedirs(os.path.dirname(blob_path), exist_ok=True)
    os.makedirs(os.path.dirname(pointer_path), exist_ok=True)
    # if passed revision is not identical to commit_hash
    # then revision has to be a branch name or tag name.
    # In that case store a ref.
    _cache_commit_hash_for_specific_revision(storage_folder, revision, commit_hash)

    if os.path.exists(pointer_path) and not force_download:
        return pointer_path

    if os.path.exists(blob_path) and not force_download:
        # we have the blob already, but not the pointer
        logger.info("creating pointer to %s from %s", blob_path, pointer_path)
        _create_relative_symlink(blob_path, pointer_path, new_blob=False)
        return pointer_path

    # Prevent parallel downloads of the same file with a lock.
    lock_path = blob_path + ".lock"

    # Some Windows versions do not allow for paths longer than 255 characters.
    # In this case, we must specify it is an extended path by using the "\\?\" prefix.
    if os.name == "nt" and len(os.path.abspath(lock_path)) > 255:
        lock_path = "\\\\?\\" + os.path.abspath(lock_path)

    if os.name == "nt" and len(os.path.abspath(blob_path)) > 255:
        blob_path = "\\\\?\\" + os.path.abspath(blob_path)

    with FileLock(lock_path):
        # If the download just completed while the lock was activated.
        if os.path.exists(pointer_path) and not force_download:
            # Even if returning early like here, the lock will be released.
            return pointer_path

        if resume_download:
            incomplete_path = blob_path + ".incomplete"

            @contextmanager
            def _resumable_file_manager() -> Generator[io.BufferedWriter, None, None]:
                with open(incomplete_path, "ab") as f:
                    yield f

            temp_file_manager = _resumable_file_manager
            if os.path.exists(incomplete_path):
                resume_size = os.stat(incomplete_path).st_size
            else:
                resume_size = 0
        else:
            temp_file_manager = partial(  # type: ignore
                tempfile.NamedTemporaryFile, mode="wb", dir=cache_dir, delete=False
            )
            resume_size = 0

        # Download to temporary file, then copy to cache dir once finished.
        # Otherwise you get corrupt cache entries if the download gets interrupted.
        with temp_file_manager() as temp_file:
            logger.info("downloading %s to %s", url, temp_file.name)

            http_get(
                url_to_download,
                temp_file,
                proxies=proxies,
                resume_size=resume_size,
                headers=headers,
            )

        logger.info("storing %s in cache at %s", url, blob_path)
        _chmod_and_replace(temp_file.name, blob_path)

        logger.info("creating pointer to %s from %s", blob_path, pointer_path)
        _create_relative_symlink(blob_path, pointer_path, new_blob=True)

    try:
        os.remove(lock_path)
    except OSError:
        pass

    return pointer_path


@validate_hf_hub_args
def try_to_load_from_cache(
    repo_id: str,
    filename: str,
    cache_dir: Union[str, Path, None] = None,
    revision: Optional[str] = None,
    repo_type: Optional[str] = None,
) -> Union[str, _CACHED_NO_EXIST_T, None]:
    """
    Explores the cache to return the latest cached file for a given revision if found.

    This function will not raise any exception if the file in not cached.

    Args:
        cache_dir (`str` or `os.PathLike`):
            The folder where the cached files lie.
        repo_id (`str`):
            The ID of the repo on huggingface.co.
        filename (`str`):
            The filename to look for inside `repo_id`.
        revision (`str`, *optional*):
            The specific model version to use. Will default to `"main"` if it's not provided and no `commit_hash` is
            provided either.
        repo_type (`str`, *optional*):
            The type of the repository. Will default to `"model"`.

    Returns:
        `Optional[str]` or `_CACHED_NO_EXIST`:
            Will return `None` if the file was not cached. Otherwise:
            - The exact path to the cached file if it's found in the cache
            - A special value `_CACHED_NO_EXIST` if the file does not exist at the given commit hash and this fact was
              cached.
    """
    if revision is None:
        revision = "main"
    if repo_type is None:
        repo_type = "model"
    if repo_type not in REPO_TYPES:
        raise ValueError(
            f"Invalid repo type: {repo_type}. Accepted repo types are:"
            f" {str(REPO_TYPES)}"
        )
    if cache_dir is None:
        cache_dir = HUGGINGFACE_HUB_CACHE

    object_id = repo_id.replace("/", "--")
    repo_cache = os.path.join(cache_dir, f"{repo_type}s--{object_id}")
    if not os.path.isdir(repo_cache):
        # No cache for this model
        return None
    for subfolder in ["refs", "snapshots"]:
        if not os.path.isdir(os.path.join(repo_cache, subfolder)):
            return None

    # Resolve refs (for instance to convert main to the associated commit sha)
    cached_refs = os.listdir(os.path.join(repo_cache, "refs"))
    if revision in cached_refs:
        with open(os.path.join(repo_cache, "refs", revision)) as f:
            revision = f.read()

    if os.path.isfile(os.path.join(repo_cache, ".no_exist", revision, filename)):
        return _CACHED_NO_EXIST

    cached_shas = os.listdir(os.path.join(repo_cache, "snapshots"))
    if revision not in cached_shas:
        # No cache for this revision and we won't try to return a random revision
        return None

    cached_file = os.path.join(repo_cache, "snapshots", revision, filename)
    return cached_file if os.path.isfile(cached_file) else None


@validate_hf_hub_args
def get_hf_file_metadata(
    url: str,
    token: Union[bool, str, None] = None,
    proxies: Optional[Dict] = None,
    timeout: float = 10,
) -> HfFileMetadata:
    """Fetch metadata of a file versioned on the Hub for a given url.

    Args:
        url (`str`):
            File url, for example returned by [`hf_hub_url`].
        token (`str` or `bool`, *optional*):
            A token to be used for the download.
                - If `True`, the token is read from the HuggingFace config
                  folder.
                - If `False` or `None`, no token is provided.
                - If a string, it's used as the authentication token.
        proxies (`dict`, *optional*):
            Dictionary mapping protocol to the URL of the proxy passed to
            `requests.request`.
        timeout (`float`, *optional*, defaults to 10):
            How many seconds to wait for the server to send metadata before giving up.

    Returns:
        A [`HfFileMetadata`] object containing metadata such as location, etag, size and
        commit_hash.
    """
    headers = build_hf_headers(token=token)

    # Retrieve metadata
    r = _request_wrapper(
        method="HEAD",
        url=url,
        headers=headers,
        allow_redirects=False,
        follow_relative_redirects=True,
        proxies=proxies,
        timeout=timeout,
    )
    hf_raise_for_status(r)

    # Return
    return HfFileMetadata(
        commit_hash=r.headers.get(HUGGINGFACE_HEADER_X_REPO_COMMIT),
        etag=_normalize_etag(
            # We favor a custom header indicating the etag of the linked resource, and
            # we fallback to the regular etag header.
            r.headers.get("ETag")
            or r.headers.get(HUGGINGFACE_HEADER_X_LINKED_ETAG)
        ),
        # Either from response headers (if redirected) or defaults to request url
        # Do not use directly `url`, as `_request_wrapper` might have followed relative
        # redirects.
        location=r.headers.get("Location") or r.request.url,  # type: ignore
        size=_int_or_none(
            r.headers.get(HUGGINGFACE_HEADER_X_LINKED_SIZE)
            or r.headers.get("Content-Length")
        ),
    )


def _int_or_none(value: Optional[str]) -> Optional[int]:
    try:
        return int(value)  # type: ignore
    except (TypeError, ValueError):
        return None


def _chmod_and_replace(src: str, dst: str) -> None:
    """Set correct permission before moving a blob from tmp directory to cache dir.

    Do not take into account the `umask` from the process as there is no convenient way
    to get it that is thread-safe.

    See:
    - About umask: https://docs.python.org/3/library/os.html#os.umask
    - Thread-safety: https://stackoverflow.com/a/70343066
    - About solution: https://github.com/huggingface/huggingface_hub/pull/1220#issuecomment-1326211591
    - Fix issue: https://github.com/huggingface/huggingface_hub/issues/1141
    - Fix issue: https://github.com/huggingface/huggingface_hub/issues/1215
    """
    # Get umask by creating a temporary file in the cached repo folder.
    tmp_file = Path(dst).parent.parent / f"tmp_{uuid.uuid4()}"
    try:
        tmp_file.touch()
        cache_dir_mode = Path(tmp_file).stat().st_mode
        os.chmod(src, stat.S_IMODE(cache_dir_mode))
    finally:
        tmp_file.unlink()

    os.replace(src, dst)
