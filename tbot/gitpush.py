"""Push bot.json to the GitHub `data` branch via the Contents API (no git binary).

The token is read only from env GITHUB_TOKEN_TOGETHER; it is never written to disk or logged.
A soft guard refuses to overwrite a fresh bot.json published by a different host, so two
instances (e.g. the box and a VPS) cannot both publish at once. Set TBOT_FORCE_PUBLISH=1
to take over after the other publisher has been stopped.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time

import aiohttp

log = logging.getLogger("tbot.git")

API = "https://api.github.com"
# Slightly longer than the 5-minute heartbeat so a live publisher is recognized as fresh.
FRESH_S = int(os.environ.get("TBOT_PUBLISH_GUARD_S", "480"))


def parse_github_repo(url: str) -> tuple[str, str]:
    """Return (owner, repo) from a github.com or api.github.com URL / SSH form."""
    s = (url or "").strip()
    m = re.search(r"api\.github\.com/repos/(?P<owner>[^/]+)/(?P<repo>[^/#?\s]+)", s)
    if not m:
        m = re.search(r"(?:^|[@/])github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/#?\s]+)", s)
    if not m:
        raise ValueError(f"cannot parse GitHub owner/repo from {url!r}")
    return m.group("owner"), m.group("repo").removesuffix(".git")


def _redact(txt: str) -> str:
    tok = os.environ.get("GITHUB_TOKEN_TOGETHER")
    if tok:
        txt = txt.replace(tok, "***")
    return txt


class DataPusher:
    def __init__(self, repo_url: str, workdir: str, branch: str = "data"):
        self.url, self.dir, self.branch = repo_url, workdir, branch
        self.owner, self.repo = parse_github_repo(repo_url)
        self.ready = False
        self.pushes = 0
        self.last_error = None
        self.last_push_t = None
        self.cmd_timeout = 60
        self._push_lock = asyncio.Lock()
        self.blocked_other_host = False

    def _headers(self) -> dict:
        tok = os.environ.get("GITHUB_TOKEN_TOGETHER") or ""
        return {
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "together-trading-bot",
        }

    def _contents_url(self, path: str) -> str:
        return f"{API}/repos/{self.owner}/{self.repo}/contents/{path}"

    async def setup(self) -> bool:
        if not os.environ.get("GITHUB_TOKEN_TOGETHER"):
            self.last_error = "GITHUB_TOKEN_TOGETHER not set"
            return False
        self.ready = True
        return True

    async def _request(self, session: aiohttp.ClientSession, method: str, url: str, **kw):
        timeout = aiohttp.ClientTimeout(total=self.cmd_timeout)
        try:
            async with session.request(method, url, headers=self._headers(), timeout=timeout, **kw) as resp:
                body = await resp.text()
                return resp.status, body
        except Exception as e:
            raise type(e)(_redact(repr(e))) from None

    async def _get_file(self, session, path: str):
        url = self._contents_url(path) + f"?ref={self.branch}"
        status, body = await self._request(session, "GET", url)
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(_redact(f"GET {path} HTTP {status}: {body[-300:]}"))
        return json.loads(body)

    def _decode_json(self, meta: dict | None):
        if not meta or not meta.get("content"):
            return None
        raw = base64.b64decode(meta["content"].replace("\n", ""))
        try:
            return json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            return None

    def _other_host_is_fresh(self, remote: dict | None, local: bytes) -> str | None:
        """Return a reason string if we must not overwrite a live publisher, else None."""
        if os.environ.get("TBOT_FORCE_PUBLISH") == "1":
            return None
        if not remote:
            return None
        try:
            ours = json.loads(local.decode())
        except (ValueError, UnicodeDecodeError):
            ours = {}
        our_host = ours.get("publisher_host")
        their_host = remote.get("publisher_host")
        # Same host (restart on the same machine) may always update.
        if our_host and their_host and our_host == their_host:
            return None
        gen = remote.get("generated")
        try:
            age = time.time() - float(gen) if gen is not None else None
        except (TypeError, ValueError):
            age = None
        if age is not None and age <= FRESH_S:
            who = their_host or "unknown-host"
            return (f"refusing to publish: {who} updated bot.json {int(age)}s ago "
                    f"(stop the other instance, wait, or set TBOT_FORCE_PUBLISH=1)")
        return None

    async def _put_file(self, session, path: str, data: bytes, message: str, sha: str | None):
        payload = {
            "message": message,
            "content": base64.b64encode(data).decode(),
            "branch": self.branch,
            "committer": {"name": "together-trading-bot", "email": "bot@users.noreply.github.com"},
        }
        if sha:
            payload["sha"] = sha
        status, body = await self._request(session, "PUT", self._contents_url(path), json=payload)
        return status, body

    async def push(self, files: dict, message: str):
        """files: {relative_name: bytes}. Updates the data-branch file(s) via Contents API. Serialized."""
        async with self._push_lock:
            self.blocked_other_host = False
            try:
                if not self.ready and not await self.setup():
                    return False
                async with aiohttp.ClientSession() as session:
                    updated = False
                    for name, data in files.items():
                        meta = await self._get_file(session, name)
                        if name.endswith(".json") or name == "bot.json":
                            reason = self._other_host_is_fresh(self._decode_json(meta), data)
                            if reason:
                                self.blocked_other_host = True
                                self.last_error = reason
                                log.warning("%s", reason)
                                return False
                        sha = meta.get("sha") if meta else None
                        # Skip the PUT if content is identical (compare decoded remote bytes).
                        if meta and meta.get("content"):
                            remote_bytes = base64.b64decode(meta["content"].replace("\n", ""))
                            if remote_bytes == data:
                                continue
                        status, body = await self._put_file(session, name, data, message, sha)
                        if status in (409, 422) and sha:
                            # sha mismatch — refresh and retry once
                            meta = await self._get_file(session, name)
                            sha = meta.get("sha") if meta else None
                            if name.endswith(".json") or name == "bot.json":
                                reason = self._other_host_is_fresh(self._decode_json(meta), data)
                                if reason:
                                    self.blocked_other_host = True
                                    self.last_error = reason
                                    log.warning("%s", reason)
                                    return False
                            status, body = await self._put_file(session, name, data, message, sha)
                        if status not in (200, 201):
                            raise RuntimeError(_redact(f"PUT {name} HTTP {status}: {body[-300:]}"))
                        updated = True
                    if not updated:
                        return False
                    self.pushes += 1
                    self.last_error = None
                    return True
            except Exception as e:  # never crash the bot because of GitHub
                self.last_error = _redact(repr(e))[:200]
                log.warning("data push failed: %s", self.last_error)
                return False
