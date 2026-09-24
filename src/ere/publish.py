"""Weekly refresh pipeline and publishing to GitHub Pages.

Why the refresh runs on your own computer and not in GitHub Actions: NSE routinely blocks
requests from cloud data-centre IP ranges (GitHub's runners included), and the raw data
cache is over a gigabyte. So the Mac does the data work and pushes only the finished HTML to
a `gh-pages` branch, which GitHub Pages serves.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class StepResult:
    name: str
    ok: bool
    seconds: float
    error: str | None = None


@dataclass
class RefreshReport:
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    def summary(self) -> str:
        lines = [f"{'ok ' if s.ok else 'FAIL'} {s.name:<22} {s.seconds:7.1f}s"
                 + (f"  {s.error}" if s.error else "") for s in self.steps]
        return "\n".join(lines)


def run_steps(steps: list[tuple[str, Callable[[], object], bool]]) -> RefreshReport:
    """Run (name, fn, critical) in order. A failed critical step stops the run; a failed
    non-critical step is recorded and the run continues."""
    rep = RefreshReport()
    for name, fn, critical in steps:
        t0 = time.monotonic()
        try:
            fn()
            rep.steps.append(StepResult(name, True, time.monotonic() - t0))
        except SystemExit as e:  # typer.Exit raised inside a command
            code = e.code if isinstance(e.code, int) else 1
            ok = code in (0, None)
            rep.steps.append(StepResult(name, ok, time.monotonic() - t0,
                                        None if ok else str(e.code)))
            if not ok and critical:
                break
        except Exception as e:
            rep.steps.append(StepResult(name, False, time.monotonic() - t0,
                                        f"{type(e).__name__}: {e}"[:300]))
            if critical:
                break
    return rep


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def publish_to_gh_pages(repo: Path, site_dir: Path, remote: str = "origin",
                        branch: str = "gh-pages", push: bool = True) -> str:
    """Commit the contents of site_dir as the whole of `branch` and push it.
    Returns the new commit id. The main branch and working tree are never touched."""
    html = sorted(site_dir.glob("*.html"))
    if not html:
        raise FileNotFoundError(f"no HTML files in {site_dir}: run `ere report --all` first")
    has_remote_branch = _git(["ls-remote", "--exit-code", "--heads", remote, branch], repo,
                             check=False).returncode == 0
    tmp = Path(tempfile.mkdtemp(prefix="ere-pages-"))
    wt = tmp / "wt"
    try:
        if has_remote_branch:
            _git(["fetch", remote, f"{branch}:refs/remotes/{remote}/{branch}"], repo)
            _git(["worktree", "add", "-B", branch, str(wt), f"{remote}/{branch}"], repo)
        else:
            _git(["worktree", "add", "--detach", str(wt)], repo)
            _git(["checkout", "--orphan", branch], wt)
        # replace everything with the new site
        for p in wt.iterdir():
            if p.name == ".git":
                continue
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        for f in html:
            shutil.copy2(f, wt / f.name)
        (wt / ".nojekyll").write_text("")
        _git(["add", "-A"], wt)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        if _git(["diff", "--cached", "--quiet"], wt, check=False).returncode == 0 \
                and has_remote_branch:
            return _git(["rev-parse", "HEAD"], wt).stdout.strip()
        _git(["commit", "-m", f"Publish reports {stamp}"], wt)
        sha = _git(["rev-parse", "HEAD"], wt).stdout.strip()
        if push:
            _git(["push", remote, f"{branch}:{branch}"], wt)
        return sha
    finally:
        _git(["worktree", "remove", "--force", str(wt)], repo, check=False)
        shutil.rmtree(tmp, ignore_errors=True)
