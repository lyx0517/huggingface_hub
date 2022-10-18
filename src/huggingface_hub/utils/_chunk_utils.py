"""Miscellaneous / general utilities"""
from typing import Any, Iterable, Iterator, List


def chunk_iterable(
    iterable: Iterable[Any],
    chunk_size: int,
) -> Iterator[List[Any]]:
    """
    Returns an iterator over iterable in chunks of size chunk_size.
    chunk_size must be a strictly positive integer (>0).
    The last chunk can be smaller than `chunk_size`.

    Raises: `ValueError` if `chunk_size` <= 0

    Examples:
        Chunking an iterable in chunks of size 8:
        ```python
        >>> iterable = range(128)
        >>> chunked_iterable = chunk_iterable(iterable, chunk_size=8)
        >>> next(chunked_iterable)
        # [0, 1, 2, 3, 4, 5, 6, 7]
        >>> next(chunked_iterable)
        # [8, 9, 10, 11, 12, 13, 14, 15]
        ```
    """

    def _chunk_iter(itr: Iterable[Any]) -> Iterator[List[Any]]:
        while True:
            chunk = [x for _, x in zip(range(chunk_size), itr)]
            if not chunk:
                break
            yield chunk

    if not chunk_size > 0:
        raise ValueError("chunk_size must be a strictly positive (>0) integer")
    return _chunk_iter(iter(iterable))
