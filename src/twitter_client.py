from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import tweepy

TWITTER_RETRIES = 2


def post_tweet(text: str, *, media_path: str | Path | None = None) -> None:
    if os.getenv("DRY_RUN", "1") == "1":
        print(f"[DRY RUN] Would tweet:\n{text}\n")
        if media_path:
            print(f"[DRY RUN] With media: {media_path}")
        return

    api_key = os.environ["TWITTER_API_KEY"]
    api_secret = os.environ["TWITTER_API_SECRET"]
    access_token = os.environ["TWITTER_ACCESS_TOKEN"]
    access_secret = os.environ["TWITTER_ACCESS_TOKEN_SECRET"]

    client = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret,
    )

    media_ids, note = _upload_chart(
        api_key, api_secret, access_token, access_secret, media_path
    )
    try:
        _create_tweet(client, text, media_ids)
    except tweepy.TweepyException as exc:
        # A chart attach can draw a 403 while the same text is allowed.
        # Post the text so one refused image does not fail the hourly run.
        if media_ids and _status_code(exc) == 403:
            print(
                f"Tweet with media refused ({_twitter_error_detail(exc)}); retrying text-only",
                file=sys.stderr,
            )
            try:
                _create_tweet(client, text, None)
            except tweepy.TweepyException as text_exc:
                print(f"Tweet failed: {_twitter_error_detail(text_exc)}", file=sys.stderr)
                raise
            print("Tweet posted. (text-only; X refused the chart)")
            return
        print(f"Tweet failed: {_twitter_error_detail(exc)}", file=sys.stderr)
        raise

    if media_ids:
        print(f"Tweet posted. (media: {media_path})")
    elif note:
        print(f"Tweet posted. ({note})")
    else:
        print("Tweet posted.")


def _upload_chart(
    api_key: str,
    api_secret: str,
    access_token: str,
    access_secret: str,
    media_path: str | Path | None,
) -> tuple[list[int] | None, str | None]:
    if not media_path:
        return None, None
    path = Path(media_path)
    if not path.exists():
        return None, None
    try:
        auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
        api = tweepy.API(auth)
        uploaded = api.media_upload(filename=str(path))
    except tweepy.TweepyException as exc:
        print(
            f"Media upload failed ({_twitter_error_detail(exc)}); posting text-only",
            file=sys.stderr,
        )
        return None, "text-only; media upload failed"
    return [uploaded.media_id], None


def _create_tweet(client: tweepy.Client, text: str, media_ids: list[int] | None) -> None:
    last_err: Exception | None = None
    for attempt in range(TWITTER_RETRIES + 1):
        try:
            client.create_tweet(text=text[:280], media_ids=media_ids)
            return
        except tweepy.TweepyException as exc:
            last_err = exc
            if attempt < TWITTER_RETRIES and _is_transient_twitter_error(exc):
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
    if last_err:
        raise last_err


def _status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    code = getattr(response, "status_code", None)
    if code is None:
        code = getattr(response, "status", None)
    return code


def _twitter_error_detail(exc: Exception) -> str:
    """Status, title, detail, problem type, and access level from an X error."""
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    headers = getattr(response, "headers", None) or {}
    access = headers.get("x-access-level") if hasattr(headers, "get") else None
    title = detail = problem = None
    json_fn = getattr(response, "json", None)
    body = None
    if callable(json_fn):
        try:
            body = json_fn()
        except Exception:
            body = None
    if isinstance(body, dict):
        title = body.get("title")
        detail = body.get("detail")
        problem = body.get("type")
    parts: list[str] = []
    status = _status_code(exc)
    if status is not None:
        parts.append(f"status={status}")
    if title:
        parts.append(f"title={title}")
    if detail:
        parts.append(f"detail={detail}")
    if problem:
        parts.append(f"type={problem}")
    if access:
        parts.append(f"x-access-level={access}")
    if len(parts) <= 1:
        text = getattr(response, "text", None) or str(exc)
        extra = str(text).strip().replace("\n", " ")
        if extra:
            parts.append(extra[:300])
    return " ".join(parts) if parts else str(exc)


def _is_transient_twitter_error(exc: Exception) -> bool:
    if _status_code(exc) in (429, 500, 502, 503, 504):
        return True
    msg = str(exc).lower()
    return "rate limit" in msg or "over capacity" in msg or "timeout" in msg
