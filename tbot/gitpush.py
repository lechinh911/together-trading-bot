"""Push the dashboard JSON to the `data` branch of the GitHub repo (plain git, no AI). The token is read by git
through GIT_ASKPASS (scripts/git_askpass.sh prints $GITHUB_TOKEN_TOGETHER); it is never written to disk or logged."""
from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger("tbot.git")
ASKPASS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "git_askpass.sh")


class DataPusher:
    def __init__(self, repo_url: str, workdir: str, branch: str = "data"):
        self.url, self.dir, self.branch = repo_url, workdir, branch
        self.ready = False
        self.pushes = 0
        self.last_error = None
        self.last_push_t = None

    def _env(self):
        return dict(os.environ, GIT_ASKPASS=ASKPASS, GIT_TERMINAL_PROMPT="0",
                    GIT_AUTHOR_NAME="together-trading-bot", GIT_AUTHOR_EMAIL="bot@users.noreply.github.com",
                    GIT_COMMITTER_NAME="together-trading-bot", GIT_COMMITTER_EMAIL="bot@users.noreply.github.com")

    async def _git(self, *args, cwd=None, check=True):
        p = await asyncio.create_subprocess_exec("git", *args, cwd=cwd or self.dir, env=self._env(),
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), 120)
        txt = out.decode(errors="replace")
        tok = os.environ.get("GITHUB_TOKEN_TOGETHER")
        if tok:
            txt = txt.replace(tok, "***")
        if check and p.returncode != 0:
            raise RuntimeError(f"git {args[0]} rc={p.returncode}: {txt.strip()[-300:]}")
        return p.returncode, txt

    async def setup(self):
        if not os.environ.get("GITHUB_TOKEN_TOGETHER"):
            self.last_error = "GITHUB_TOKEN_TOGETHER not set"
            return False
        if not os.path.isdir(os.path.join(self.dir, ".git")):
            os.makedirs(self.dir, exist_ok=True)
            await self._git("init", "-q")
            await self._git("remote", "add", "origin", self.url)
        rc, _ = await self._git("fetch", "-q", "origin", self.branch, check=False)
        if rc == 0:
            await self._git("checkout", "-q", "-B", self.branch, f"origin/{self.branch}")
        else:
            await self._git("checkout", "-q", "--orphan", self.branch, check=False)
        self.ready = True
        return True

    async def push(self, files: dict, message: str):
        """files: {relative_name: bytes}. Commits only if something changed."""
        try:
            if not self.ready and not await self.setup():
                return False
            for name, data in files.items():
                p = os.path.join(self.dir, name)
                with open(p + ".tmp", "wb") as f:
                    f.write(data)
                os.replace(p + ".tmp", p)
            await self._git("add", "-A")
            rc, _ = await self._git("diff", "--cached", "--quiet", check=False)
            if rc == 0:
                return False
            await self._git("commit", "-q", "-m", message)
            rc, txt = await self._git("push", "-q", "origin", f"HEAD:{self.branch}", check=False)
            if rc != 0:  # remote moved (should not happen): rebase onto it and retry once
                await self._git("pull", "-q", "--rebase", "-X", "theirs", "origin", self.branch, check=False)
                await self._git("push", "-q", "origin", f"HEAD:{self.branch}")
            self.pushes += 1
            self.last_error = None
            return True
        except Exception as e:  # never crash the bot because of GitHub
            self.last_error = str(e)[:200]
            log.warning("data push failed: %s", self.last_error)
            return False
