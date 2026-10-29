"""Records: refs/alock/<target> names the commit each target was last applied
from. In remote mode origin holds them too, fetched into refs/alock-origin/.

Rewrites: every applied commit gets a note in refs/notes/alock, which git
copies onto the commit's rewrites when GIT_NOTES_REWRITE_REF names it. Each
copied note becomes refs/alock-rewrites/<applied>/<rewrite>, which keeps the
rewrite reachable and travels to origin with the records."""

from __future__ import annotations

import fcntl
import re
import subprocess
import time

from .common import REMOTE, AlockError, Repo, git, network_env, target_key

LOCAL_NS = "refs/alock"
ORIGIN_NS = "refs/alock-origin"
REWRITES_NS = "refs/alock-rewrites"
NOTES_REF = "refs/notes/alock"
APPLIED = re.compile(r"applied ([0-9a-f]{40}(?:[0-9a-f]{24})?)$")


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
    """HEAD contains a record when it contains the record or a rewrite that
    carries all of it, either as an ancestor or as a change HEAD holds.
    DESIGN.md's Containment section says why."""
    line = [record] + [r for r in rewrites(repo, record) if carries(repo, record, r)]
    if any(is_ancestor(repo, commit, head) for commit in line):
        return True
    return any(holds(repo, head, commit) for commit in line)


def is_ancestor(repo: Repo, commit: str, head: str) -> bool:
    proc = git("merge-base", "--is-ancestor", commit, head, cwd=repo.top, check=False)
    return proc.returncode == 0


def carries(repo: Repo, applied: str, rewrite: str) -> bool:
    """Whether a rewrite carries all of what applying its commit put live.
    Its note vouches for the commit's own change; every other commit that
    applied has and rewrite lacks, such as one a rebase left behind, needs a
    rewrite of its own in rewrite's history, or its change held there."""
    left = git("rev-list", applied, "--not", rewrite, cwd=repo.top, check=False)
    if left.returncode != 0:
        return False
    for commit in left.stdout.split():
        if commit == applied:
            continue
        if any(is_ancestor(repo, r, rewrite) for r in rewrites(repo, commit)):
            continue
        if not holds(repo, rewrite, commit):
            return False
    return True


def holds(repo: Repo, head: str, commit: str) -> bool:
    """Whether HEAD holds commit's change: merging commit into HEAD, or into a
    copy of commit in HEAD's history since their merge base, changes nothing.

    A copy has commit's author, author date and subject, as rebases and
    cherry-picks keep them. It was made from commit, so anything since that
    undid its change was made on top of it. A commit that merely makes the same
    change, and was undone later, says nothing about commit."""
    base = git("merge-base", head, commit, cwd=repo.top, check=False).stdout.strip()
    if not base:
        return False
    fmt = "--format=%H%x00%an%x00%ae%x00%ad%x00%s"
    own = git("log", "-1", fmt, "--date=raw", commit, cwd=repo.top, check=False)
    since = git("log", fmt, "--date=raw", f"{base}..{head}", cwd=repo.top, check=False)
    identity = own.stdout.strip().split("\0")[1:]
    tried = [head]
    for line in since.stdout.splitlines():
        sha, *rest = line.split("\0")
        if rest == identity:
            tried.append(sha)
    for candidate in dict.fromkeys(tried):
        merged = git("merge-tree", "--write-tree", candidate, commit, cwd=repo.top, check=False)
        if merged.returncode != 0:
            continue
        tree = git("rev-parse", f"{candidate}^{{tree}}", cwd=repo.top).stdout.strip()
        if merged.stdout.split("\n", 1)[0].strip() == tree:
            return True
    return False


def rewrite_ref(applied: str, rewrite: str) -> str:
    return f"{REWRITES_NS}/{applied}/{rewrite}"


def rewrites(repo: Repo, applied: str) -> list[str]:
    """The known rewrites of an applied commit. A ref whose name doesn't match
    the commit it points at is ignored."""
    proc = git(
        "for-each-ref", "--format=%(refname) %(objectname)", f"{REWRITES_NS}/{applied}/",
        cwd=repo.top, check=False,
    )
    found = []
    for line in proc.stdout.splitlines():
        ref, _, sha = line.partition(" ")
        if ref.rsplit("/", 1)[-1] == sha:
            found.append(sha)
    return found


NOTE_LIMIT = 1000  # commits noted per apply; the most recent come first


def mark_applied(repo: Repo, head: str) -> None:
    """Note head, and each commit it brings that no record here reaches yet,
    as applied, so git copies each note onto that commit's rewrites. A rebase
    that conflicts in an earlier commit then still links that commit."""
    fresh = git(
        "rev-list", f"--max-count={NOTE_LIMIT}", head, "--not", f"--glob={LOCAL_NS}/*",
        cwd=repo.top,
    ).stdout.split()
    note_applied(repo, [head, *fresh])


def note_applied(repo: Repo, commits: list[str]) -> int:
    """Give each commit its `applied` note, and return how many lacked one."""
    commits = list(dict.fromkeys(commits))
    repo.state_dir.mkdir(parents=True, exist_ok=True)
    with open(repo.state_dir / "notes.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # A rebase in another worktree can copy notes meanwhile; fast-import
        # then refuses to move the ref, and this tries again.
        for _ in range(3):
            written = write_notes(repo, commits)
            if written is not None:
                return written
    raise AlockError(f"alock: couldn't write {NOTES_REF}, so nothing ran. Try again.")


def write_notes(repo: Repo, commits: list[str]) -> int | None:
    """How many notes were written, or None when another writer got there first."""
    old = read_notes_ref(repo)
    notes = read_notes(repo)
    changed = {}
    for commit in commits:
        text, line = notes.get(commit, ""), f"applied {commit}"
        if line not in text.splitlines():
            changed[commit] = (text.rstrip("\n") + "\n\n" if text.strip() else "") + line + "\n"
    if not changed:
        return 0
    message = b"alock: note applied commits\n"
    stream = [
        f"commit {NOTES_REF}\ncommitter alock <alock@localhost> {int(time.time())} +0000\n".encode(),
        b"data %d\n%s" % (len(message), message),
    ]
    if old:
        stream.append(f"from {old}\n".encode())
    for commit, text in changed.items():
        data = text.encode()
        stream.append(b"N inline %s\ndata %d\n%s\n" % (commit.encode(), len(data), data))
    proc = subprocess.run(
        ["git", "fast-import", "--quiet"], cwd=repo.top, input=b"".join(stream), capture_output=True,
    )
    return len(changed) if proc.returncode == 0 else None


def records_in(repo: Repo) -> list[tuple[str, str]]:
    """Every record in this clone, its own and origin's, as (ref, commit)."""
    refs = git(
        "for-each-ref", "--format=%(refname) %(objectname)", LOCAL_NS, ORIGIN_NS, cwd=repo.top,
    ).stdout.splitlines()
    return [tuple(line.split(" ", 1)) for line in refs]


def unnoted(repo: Repo) -> list[tuple[str, str]]:
    """The records whose commit has no `applied` note, as (ref, commit): applied
    before alock followed rebases, so a rebase of them wouldn't be followed."""
    notes = read_notes(repo)
    return [
        (ref, sha) for ref, sha in records_in(repo)
        if f"applied {sha}" not in notes.get(sha, "").splitlines()
    ]


def read_notes_ref(repo: Repo) -> str | None:
    proc = git("rev-parse", "--verify", "-q", NOTES_REF, cwd=repo.top, check=False)
    return proc.stdout.strip() or None


def read_notes(repo: Repo) -> dict[str, str]:
    """Every note in refs/notes/alock, as {annotated commit: text}."""
    listing = git("notes", f"--ref={NOTES_REF}", "list", cwd=repo.top, check=False).stdout.split()
    pairs = list(zip(listing[::2], listing[1::2]))  # (note blob, annotated commit)
    if not pairs:
        return {}
    texts = cat_file(repo, [blob for blob, _ in pairs])
    return {commit: texts.get(blob, "") for blob, commit in pairs}


def harvest(repo: Repo) -> None:
    """Turn each note git copied onto a rewrite into its rewrite ref."""
    notes = read_notes(repo)
    if not notes:
        return
    existing = cat_file(repo, list(notes), contents=False)
    have = set(git("for-each-ref", "--format=%(refname)", REWRITES_NS, cwd=repo.top).stdout.split())
    lines = []
    for commit, note in notes.items():
        if existing.get(commit) != "commit":
            continue
        for text in note.splitlines():
            match = APPLIED.match(text.strip())
            if match and match.group(1) != commit:
                ref = rewrite_ref(match.group(1), commit)
                if ref not in have:
                    have.add(ref)
                    # Unchecked: a concurrent harvest can only write the same value.
                    lines.append(f"update {ref} {commit}")
    if lines:
        git("update-ref", "--stdin", cwd=repo.top, input="\n".join(lines) + "\n")


def cat_file(repo: Repo, names: list[str], contents: bool = True) -> dict[str, str]:
    """{name: content} for each object found, or {name: type} without contents."""
    proc = subprocess.run(
        ["git", "cat-file", "--batch" if contents else "--batch-check"],
        cwd=repo.top, input="\n".join(names).encode() + b"\n", capture_output=True,
    )
    found, out, pos = {}, proc.stdout, 0
    while pos < len(out):
        end = out.index(b"\n", pos)
        fields = out[pos:end].decode().split()
        pos = end + 1
        if len(fields) < 3:  # "<name> missing"
            continue
        if contents:
            size = int(fields[2])
            found[fields[0]] = out[pos:pos + size].decode(errors="replace")
            pos += size + 1
        else:
            found[fields[0]] = fields[1]
    return found


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


def list_origin(repo: Repo) -> tuple[dict[str, str], dict[str, str]]:
    """origin's records and rewrite refs, each as {ref: commit}."""
    proc = git(
        "ls-remote", REMOTE, f"{LOCAL_NS}/*", f"{REWRITES_NS}/*",
        cwd=repo.top, check=False, env=network_env(),
    )
    if proc.returncode != 0:
        raise AlockError(f"alock: git ls-remote {REMOTE} failed, so nothing ran:\n{proc.stderr.strip()}")
    found, rewritten = {}, {}
    for line in proc.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith(LOCAL_NS + "/"):
            found[ref] = sha
        elif ref.startswith(REWRITES_NS + "/"):
            rewritten[ref] = sha
    return found, rewritten


def fetch_origin(repo: Repo, targets: list[str] | None = None) -> dict[str, str | None]:
    """Fetch origin's records into refs/alock-origin/, with their commits, and
    the rewrite refs this clone lacks, and return origin's value for each
    target (None where origin has no record)."""
    on_origin, rewritten = list_origin(repo)
    if targets is None:
        refs = sorted(on_origin)
    else:
        refs = [local_ref(t) for t in targets]
    present = [ref for ref in refs if ref in on_origin]
    have = set(git("for-each-ref", "--format=%(refname)", REWRITES_NS, cwd=repo.top).stdout.split())
    wanted = [ref for ref in rewritten if ref not in have]
    if present or wanted:
        specs = [f"+{ref}:{ORIGIN_NS}{ref[len(LOCAL_NS):]}" for ref in present]
        specs += [f"+{ref}:{ref}" for ref in wanted]
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
    still holds the value fetched. This publishes head and its ancestry, and
    every rewrite ref this clone has with its commit."""
    leases = [f"--force-with-lease={ref}:{old or ''}" for ref, old in olds.items()]
    specs = [f"{head}:{ref}" for ref in olds] + [f"{REWRITES_NS}/*:{REWRITES_NS}/*"]
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
