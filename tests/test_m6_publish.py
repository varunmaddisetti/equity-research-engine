import subprocess

import pytest

from ere.publish import publish_to_gh_pages, run_steps


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "remote.git"
    git(["init", "--bare", "-b", "main", str(remote)], tmp_path)
    r = tmp_path / "repo"
    r.mkdir()
    git(["init", "-b", "main"], r)
    git(["config", "user.email", "t@t"], r)
    git(["config", "user.name", "t"], r)
    (r / "README.md").write_text("hi")
    git(["add", "."], r)
    git(["commit", "-m", "init"], r)
    git(["remote", "add", "origin", str(remote)], r)
    git(["push", "origin", "main"], r)
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>v1</h1>")
    (site / "A.html").write_text("a")
    (site / "_errors.txt").write_text("not published")
    return r, remote, site


def test_first_publish_creates_orphan_branch(repo):
    r, remote, site = repo
    main_before = git(["rev-parse", "main"], r)
    publish_to_gh_pages(r, site)
    files = git(["ls-tree", "--name-only", "gh-pages"], remote).split()
    assert set(files) == {".nojekyll", "A.html", "index.html"}
    assert git(["rev-parse", "main"], r) == main_before
    assert git(["status", "--porcelain"], r) == ""          # working tree untouched
    # orphan: no shared history with main
    assert git(["rev-list", "--count", "gh-pages"], remote) == "1"


def test_second_publish_adds_commit_and_removes_stale_files(repo):
    r, remote, site = repo
    publish_to_gh_pages(r, site)
    (site / "A.html").unlink()
    (site / "index.html").write_text("<h1>v2</h1>")
    publish_to_gh_pages(r, site)
    assert git(["rev-list", "--count", "gh-pages"], remote) == "2"
    assert "A.html" not in git(["ls-tree", "--name-only", "gh-pages"], remote)
    assert git(["show", "gh-pages:index.html"], remote) == "<h1>v2</h1>"


def test_no_push(repo):
    r, remote, site = repo
    publish_to_gh_pages(r, site, push=False)
    with pytest.raises(subprocess.CalledProcessError):
        git(["rev-parse", "gh-pages"], remote)


def test_run_steps_stops_on_critical_failure_only():
    calls = []

    def ok(name):
        return lambda: calls.append(name)

    def boom():
        raise RuntimeError("nse down")

    rep = run_steps([("a", ok("a"), True), ("b", boom, False), ("c", ok("c"), True),
                     ("d", boom, True), ("e", ok("e"), True)])
    assert calls == ["a", "c"]
    assert [s.ok for s in rep.steps] == [True, False, True, False]
    assert not rep.ok and "nse down" in rep.summary()


def test_run_steps_treats_exit_zero_as_success():
    def exit0():
        raise SystemExit(0)

    assert run_steps([("x", exit0, True)]).ok
