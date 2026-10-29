"""Records: refs/alock/<target> names the commit each target was last applied
from. In remote mode origin holds them too, fetched into refs/alock-origin/."""

from __future__ import annotations

from .common import REMOTE, AlockError, Repo, git, network_env, target_key

LOCAL_NS = "refs/alock"
ORIGIN_NS = "refs/alock-origin"


def local_ref(target: str) -> str:
    return f"{LOCAL_NS}/{target_key(target)}"


def origin_ref(target: str) -> str:
    return f"{ORIGIN_NS}/{target_key(target)}"


def read_ref(repo: Repo, ref: str) -> str | None:
    proc = git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", cwd=repo.top, check=False)
    return proc.stdout.strip() or None


def records(repo: Repo, target: str) -> list[tuple[str, str]]:
    """Every record of the target this clone knows, as (ref, commit)."""
    found = []
    for ref in (local_ref(target), origin_ref(target)):
        sha = read_ref(repo, ref)
        if sha:
            found.append((ref, sha))
    return found


def contains(repo: Repo, head: str, record: str) -> bool:
    """HEAD contains a record when the record is its ancestor, or when merging
    the record into HEAD changes nothing (a rebased or squashed apply)."""
    proc = git("merge-base", "--is-ancestor", record, head, cwd=repo.top, check=False)
    if proc.returncode == 0:
        return True
    merged = git("merge-tree", "--write-tree", head, record, cwd=repo.top, check=False)
    if merged.returncode != 0:
        return False
    head_tree = git("rev-parse", f"{head}^{{tree}}", cwd=repo.top).stdout.strip()
    return merged.stdout.split("\n", 1)[0].strip() == head_tree


def describe_commit(repo: Repo, sha: str) -> str:
    proc = git("log", "-1", "--format=%h %s (%an, %cr)", sha, cwd=repo.top, check=False)
    return proc.stdout.strip() or sha[:12]


def zero_oid(repo: Repo) -> str:
    fmt = git("rev-parse", "--show-object-format", cwd=repo.top, check=False).stdout.strip()
    return "0" * (64 if fmt == "sha256" else 40)


def update_local(repo: Repo, new: str, olds: dict[str, str | None]) -> None:
    """Set each ref to new in one transaction, checking each one's old value."""
    zero = zero_oid(repo)
    lines = ["start"]
    lines += [f"update {ref} {new} {old or zero}" for ref, old in olds.items()]
    lines += ["prepare", "commit", ""]
    proc = git("update-ref", "--stdin", cwd=repo.top, check=False, input="\n".join(lines))
    if proc.returncode != 0:
        raise AlockError(
            "alock: a record changed while this call held its lock, so a lock was "
            f"bypassed. Nothing ran.\n{proc.stderr.strip()}"
        )


def list_origin(repo: Repo) -> dict[str, str]:
    """origin's records, as {ref: commit}."""
    proc = git("ls-remote", REMOTE, f"{LOCAL_NS}/*", cwd=repo.top, check=False, env=network_env())
    if proc.returncode != 0:
        raise AlockError(f"alock: git ls-remote {REMOTE} failed, so nothing ran:\n{proc.stderr.strip()}")
    found = {}
    for line in proc.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith(LOCAL_NS + "/"):
            found[ref] = sha
    return found


def fetch_origin(repo: Repo, targets: list[str] | None = None) -> dict[str, str | None]:
    """Fetch origin's records into refs/alock-origin/, with their commits, and
    return origin's value for each target (None where origin has no record)."""
    on_origin = list_origin(repo)
    if targets is None:
        refs = sorted(on_origin)
    else:
        refs = [local_ref(t) for t in targets]
    present = [ref for ref in refs if ref in on_origin]
    if present:
        specs = [f"+{ref}:{ORIGIN_NS}{ref[len(LOCAL_NS):]}" for ref in present]
        proc = git(
            "fetch", "--no-tags", "--no-write-fetch-head", "--quiet", REMOTE, *specs,
            cwd=repo.top, check=False, env=network_env(),
        )
        if proc.returncode != 0:
            raise AlockError(f"alock: fetching {REMOTE}'s records failed, so nothing ran:\n{proc.stderr.strip()}")
    for ref in refs:
        if ref not in on_origin:
            git("update-ref", "-d", f"{ORIGIN_NS}{ref[len(LOCAL_NS):]}", cwd=repo.top, check=False)
    return {ref: on_origin.get(ref) for ref in refs}


def push_records(repo: Repo, head: str, olds: dict[str, str | None]) -> None:
    """Point each of origin's records at head, atomically, each only if it
    still holds the value fetched. This publishes head and its ancestry."""
    leases = [f"--force-with-lease={ref}:{old or ''}" for ref, old in olds.items()]
    specs = [f"{head}:{ref}" for ref in olds]
    proc = git(
        "push", "--atomic", "--porcelain", *leases, REMOTE, *specs,
        cwd=repo.top, check=False, env=network_env(),
    )
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).strip()
        if "stale info" in detail:
            raise AlockError(
                f"alock: a record on {REMOTE} changed while this call held its lock, so "
                f"a lock was bypassed. Nothing ran.\n{detail}"
            )
        raise AlockError(f"alock: pushing the records to {REMOTE} failed, so nothing ran:\n{detail}")
