#!/usr/bin/env python3
"""
Production-grade Repo Orphan Check for Flux + Kustomize repos.

What it does
------------
1) Discovers Flux Kustomization roots from repo YAML files (kind=Kustomization with
   apiVersion containing ".toolkit.fluxcd.io/") and uses spec.path as graph roots.
   (Or you can pass --roots explicitly.)

2) Walks each root's kustomization.yaml/yml/Kustomization recursively and collects
   all *reachable files* via:
   - resources / bases / components
   - patchesStrategicMerge
   - patches (path)
   - transformers / generators / crds
   - configMapGenerator/secretGenerator files/envs

3) Computes repo "workload manifest candidates" (git-tracked *.yml/*.yaml by default),
   then reports those candidates that are *unreachable* => repo orphans.

Important production behavior
-----------------------------
- Flux "control-plane" YAMLs (Flux toolkit objects) are NOT treated as workload manifests,
  and therefore are excluded from orphan evaluation.
- If a YAML file mixes Flux toolkit objects and non-Flux Kubernetes objects, this script
  fails with an error (clean repo hygiene; avoid ambiguous ownership).

Exit codes
---------
0: OK (or warn mode with orphans)
1: Orphans found (fail mode)
2: Errors (missing referenced files, invalid dir refs, mixed Flux+workload in same file, parse errors)
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

try:
    import yaml  # type: ignore
except ImportError:
    print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


KUSTOMIZATION_FILENAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
CANDIDATE_EXTS = (".yaml", ".yml")

DEFAULT_IGNORE_GLOBS = [
    ".git/**",
    ".github/**",
    ".gitlab/**",
    ".vscode/**",
    ".idea/**",
    "node_modules/**",
    "vendor/**",
    "dist/**",
    "build/**",
    "target/**",
    "**/*.md",
    "**/*.txt",
    "**/*.rst",
    "**/*.png",
    "**/*.jpg",
    "**/*.jpeg",
    "**/*.gif",
    "**/*.svg",
    "**/*.pdf",
    "**/*.lock",
    "**/*.sum",
    "**/*.zip",
    "**/*.tar",
    "**/*.tgz",
    "**/*.gz",
]


@dataclass(frozen=True)
class Ref:
    src_kustomization: Path
    ref_path: Path
    ref_kind: str  # resources/bases/components/patches/transformers/generators/cmgen/secgen/crds


@dataclass
class Report:
    roots: List[str]
    reachable_files: List[str]

    orphan_files: List[str]
    orphan_kustomizations: List[str]

    missing_refs: List[Dict[str, str]]
    invalid_dir_refs: List[Dict[str, str]]
    mixed_flux_and_workload_files: List[str]
    errors: List[str]


# -------------------------------
# Small helpers
# -------------------------------

def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def rel(repo: Path, p: Path) -> str:
    try:
        return str(p.relative_to(repo)).replace(os.sep, "/")
    except ValueError:
        return str(p).replace(os.sep, "/")


def run(cmd: List[str], cwd: Path) -> Tuple[int, str]:
    p = subprocess.run(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if p.returncode != 0 and err:
        out = (out + "\n" + err).strip()
    return p.returncode, out


def load_ignore_globs(repo: Path, ignore_file: str) -> List[str]:
    p = repo / ignore_file
    if not p.exists():
        return []
    globs: List[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        globs.append(s)
    return globs


def is_ignored(repo: Path, p: Path, ignore_globs: List[str]) -> bool:
    rp = rel(repo, p)
    for g in ignore_globs:
        if fnmatch.fnmatch(rp, g):
            return True
    return False


# -------------------------------
# Git file listing
# -------------------------------

def git_tracked_files(repo: Path) -> List[Path]:
    code, out = run(["git", "ls-files"], cwd=repo)
    if code != 0:
        raise RuntimeError(f"git ls-files failed:\n{out}")
    return [(repo / line.strip()).resolve() for line in out.splitlines() if line.strip()]


def git_changed_files(repo: Path, base_ref: str) -> Set[Path]:
    code, out = run(["git", "diff", "--name-only", f"{base_ref}...HEAD"], cwd=repo)
    if code != 0:
        raise RuntimeError(f"git diff failed:\n{out}")
    return {(repo / line.strip()).resolve() for line in out.splitlines() if line.strip()}


# -------------------------------
# YAML classification: Flux control-plane vs workload
# -------------------------------

def _load_yaml_docs(p: Path) -> List[Any]:
    try:
        return list(yaml.safe_load_all(p.read_text(encoding="utf-8")))
    except Exception:
        return []


def is_flux_toolkit_obj(d: Any) -> bool:
    if not isinstance(d, dict):
        return False
    api = str(d.get("apiVersion", ""))
    return ".toolkit.fluxcd.io/" in api


def is_k8s_obj(d: Any) -> bool:
    # "Kubernetes object" heuristic: has apiVersion + kind
    return isinstance(d, dict) and ("apiVersion" in d) and ("kind" in d)


def classify_yaml_file_flux_vs_workload(p: Path) -> Tuple[bool, bool]:
    """
    Returns (has_flux_objects, has_non_flux_k8s_objects).
    Non-flux is counted only for documents that look like k8s objects.
    """
    docs = _load_yaml_docs(p)
    has_flux = False
    has_non_flux_k8s = False
    for d in docs:
        if not is_k8s_obj(d):
            continue
        if is_flux_toolkit_obj(d):
            has_flux = True
        else:
            has_non_flux_k8s = True
    return has_flux, has_non_flux_k8s


def is_flux_control_plane_only_yaml(p: Path) -> bool:
    has_flux, has_non_flux = classify_yaml_file_flux_vs_workload(p)
    return has_flux and not has_non_flux


# -------------------------------
# Flux roots discovery (Kustomization spec.path)
# -------------------------------

def discover_flux_kustomization_roots(repo: Path, yaml_files: Iterable[Path]) -> List[Path]:
    roots: List[Path] = []
    seen: Set[str] = set()
    for f in yaml_files:
        docs = _load_yaml_docs(f)
        for d in docs:
            if not isinstance(d, dict):
                continue
            api = str(d.get("apiVersion", ""))
            kind = str(d.get("kind", ""))
            if kind != "Kustomization":
                continue
            if not api.startswith("kustomize.toolkit.fluxcd.io/"):
                continue
            spec = d.get("spec")
            if not isinstance(spec, dict):
                continue
            sp = spec.get("path")
            if not isinstance(sp, str) or not sp.strip():
                continue
            rp = sp.strip()
            # Normalize leading "./"
            if rp.startswith("./"):
                rp = rp[2:]
            if rp == "":
                rp = "."
            if rp in seen:
                continue
            seen.add(rp)
            roots.append((repo / rp).resolve())

    return roots


# -------------------------------
# Kustomize graph walking
# -------------------------------

def locate_kustomization_file(dir_path: Path) -> Optional[Path]:
    for name in KUSTOMIZATION_FILENAMES:
        p = dir_path / name
        if p.exists() and p.is_file():
            return p
    return None


def is_remote_ref(raw: str) -> bool:
    return bool(re.match(r"^(https?|git|oci)://", raw.strip()))


def normalize_ref_path(repo: Path, base_dir: Path, raw: str) -> Path:
    s = raw.strip()
    if not s:
        return base_dir.resolve()
    if is_remote_ref(s):
        return Path(s)
    p = Path(s)
    if p.is_absolute():
        return p  # treated as outside/invalid later
    return (base_dir / p).resolve()


def parse_kustomization_refs(repo: Path, kfile: Path) -> List[Ref]:
    base_dir = kfile.parent
    data = yaml.safe_load(kfile.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return []

    refs: List[Ref] = []

    def add_list(key: str, kind: str) -> None:
        v = data.get(key)
        if isinstance(v, list):
            for it in v:
                if isinstance(it, str):
                    refs.append(Ref(kfile, normalize_ref_path(repo, base_dir, it), kind))

    # Base inclusion
    add_list("resources", "resources")
    add_list("bases", "bases")
    add_list("components", "components")

    # Patches
    add_list("patchesStrategicMerge", "patchesStrategicMerge")
    pv = data.get("patches")
    if isinstance(pv, list):
        for it in pv:
            if isinstance(it, str):
                refs.append(Ref(kfile, normalize_ref_path(repo, base_dir, it), "patches"))
            elif isinstance(it, dict):
                path = it.get("path")
                if isinstance(path, str) and path.strip():
                    refs.append(Ref(kfile, normalize_ref_path(repo, base_dir, path), "patches"))

    # Transformers / generators / crds
    add_list("transformers", "transformers")
    add_list("generators", "generators")
    add_list("crds", "crds")

    # configMapGenerator / secretGenerator input files/envs
    for gen_key, prefix in [("configMapGenerator", "configMapGenerator"), ("secretGenerator", "secretGenerator")]:
        gv = data.get(gen_key)
        if not isinstance(gv, list):
            continue
        for entry in gv:
            if not isinstance(entry, dict):
                continue
            for files_key in ("files", "envs"):
                fv = entry.get(files_key)
                if not isinstance(fv, list):
                    continue
                for item in fv:
                    if not isinstance(item, str):
                        continue
                    s = item.strip()
                    # support "key=path"
                    if "=" in s and not s.startswith("="):
                        _, s = s.split("=", 1)
                        s = s.strip()
                    refs.append(Ref(kfile, normalize_ref_path(repo, base_dir, s), f"{prefix}.{files_key}"))

    return refs


def collect_reachable_files(
    repo: Path,
    root_dirs: List[Path],
    ignore_globs: List[str],
) -> Tuple[Set[Path], List[Dict[str, str]], List[Dict[str, str]], List[str]]:
    """
    Returns:
      reachable_files (absolute paths),
      missing_refs,
      invalid_dir_refs,
      errors
    """
    reachable: Set[Path] = set()
    missing_refs: List[Dict[str, str]] = []
    invalid_dir_refs: List[Dict[str, str]] = []
    errors: List[str] = []

    visited_kustomizations: Set[Path] = set()
    stack: List[Path] = []

    def enqueue_dir_as_kustomization(d: Path, reason: str) -> None:
        kf = locate_kustomization_file(d)
        if not kf:
            invalid_dir_refs.append({"dir": rel(repo, d), "reason": reason})
            return
        kf = kf.resolve()
        if kf in visited_kustomizations:
            return
        visited_kustomizations.add(kf)
        stack.append(kf)

    # initialize roots
    for rd in root_dirs:
        if not rd.exists():
            errors.append(f"Root path not found: {rel(repo, rd)}")
            continue
        if rd.is_file():
            if rd.name in KUSTOMIZATION_FILENAMES:
                kf = rd.resolve()
                if kf not in visited_kustomizations:
                    visited_kustomizations.add(kf)
                    stack.append(kf)
            else:
                errors.append(f"Root is a file but not a kustomization file: {rel(repo, rd)}")
            continue
        enqueue_dir_as_kustomization(rd.resolve(), reason="root")

    # DFS
    repo_abs = repo.resolve()
    while stack:
        kfile = stack.pop()
        if is_ignored(repo, kfile, ignore_globs):
            continue
        reachable.add(kfile)

        try:
            refs = parse_kustomization_refs(repo, kfile)
        except Exception as ex:
            errors.append(f"Failed to parse {rel(repo, kfile)}: {ex}")
            continue

        for rf in refs:
            rp = rf.ref_path

            # skip remote refs for repo-file reachability
            if is_remote_ref(str(rp)):
                continue

            # absolute paths or outside repo are treated as invalid/missing
            try:
                _ = rp.resolve().relative_to(repo_abs)
            except Exception:
                missing_refs.append({
                    "from": rel(repo, rf.src_kustomization),
                    "ref": str(rp),
                    "kind": rf.ref_kind,
                    "reason": "ref_outside_repo_or_absolute",
                })
                continue

            rp_abs = rp.resolve()
            if is_ignored(repo, rp_abs, ignore_globs):
                continue

            if not rp_abs.exists():
                missing_refs.append({
                    "from": rel(repo, rf.src_kustomization),
                    "ref": rel(repo, rp_abs),
                    "kind": rf.ref_kind,
                    "reason": "not_found",
                })
                continue

            if rp_abs.is_dir():
                enqueue_dir_as_kustomization(rp_abs, reason=f"referenced_by:{rel(repo, rf.src_kustomization)}:{rf.ref_kind}")
            else:
                reachable.add(rp_abs)

    return reachable, missing_refs, invalid_dir_refs, errors


# -------------------------------
# Candidate selection
# -------------------------------

def list_yaml_candidates(
    repo: Path,
    ignore_globs: List[str],
    use_git: bool,
    changed_since: Optional[str],
) -> List[Path]:
    if use_git:
        files = git_tracked_files(repo)
        if changed_since:
            changed = git_changed_files(repo, changed_since)
            files = [f for f in files if f in changed]
    else:
        files = [p.resolve() for p in repo.rglob("*") if p.is_file()]

    out: List[Path] = []
    for f in files:
        if f.suffix.lower() not in CANDIDATE_EXTS:
            continue
        if is_ignored(repo, f, ignore_globs):
            continue
        out.append(f.resolve())
    return out

# -------------------------------
# Main
# -------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Repo orphan check for Kustomize reachable manifests (Flux roots supported)")
    ap.add_argument("--repo", default=".", help="Repo root path (default: .)")
    ap.add_argument("--roots", nargs="*", default=None,
                    help="Explicit root dirs (repo-relative). If provided, Flux discovery is skipped.")
    ap.add_argument("--discover-flux-roots", action="store_true", default=True,
                    help="Discover Flux Kustomization spec.path roots from repo YAMLs (default: enabled)")
    ap.add_argument("--no-discover-flux-roots", dest="discover_flux_roots", action="store_false",
                    help="Disable Flux roots discovery (requires --roots)")
    ap.add_argument("--ignore-file", default=".orphanignore", help="Ignore file with glob patterns (default: .orphanignore)")
    ap.add_argument("--ignore", action="append", default=[], help="Additional ignore glob (repeatable)")
    ap.add_argument("--use-git", action="store_true", default=True, help="Use git ls-files (default: enabled)")
    ap.add_argument("--no-use-git", dest="use_git", action="store_false", help="Scan filesystem instead of git")
    ap.add_argument("--changed-since", default=None,
                    help="Only check files changed since git ref (e.g. origin/main). Requires --use-git.")
    ap.add_argument("--mode", choices=["warn", "fail"], default="fail",
                    help="warn: exit 0 on orphans; fail: exit 1 on orphans (default: fail)")
    ap.add_argument("--json", dest="json_out", action="store_true", help="Print JSON report")
    ap.add_argument("--verbose", action="store_true", help="Verbose logs to stderr")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        eprint(f"ERROR: repo is not a directory: {repo}")
        return 2

    ignore_globs = list(DEFAULT_IGNORE_GLOBS)
    ignore_globs += load_ignore_globs(repo, args.ignore_file)
    ignore_globs += (args.ignore or [])

    # candidates for discovery + orphan evaluation
    try:
        yaml_candidates = list_yaml_candidates(repo, ignore_globs, use_git=args.use_git, changed_since=None)
    except Exception as ex:
        eprint(f"ERROR: unable to list YAML candidates: {ex}")
        return 2

    # detect mixed Flux+workload yaml files (hard error in production)
    mixed_files: List[str] = []
    for f in yaml_candidates:
        has_flux, has_non_flux = classify_yaml_file_flux_vs_workload(f)
        if has_flux and has_non_flux:
            mixed_files.append(rel(repo, f))

    # roots
    root_dirs: List[Path] = []
    if args.roots:
        for r in args.roots:
            root_dirs.append((repo / r).resolve())
    else:
        if not args.discover_flux_roots:
            eprint("ERROR: no --roots provided and Flux roots discovery is disabled.")
            return 2
        root_dirs = discover_flux_kustomization_roots(repo, yaml_candidates)
        if not root_dirs:
            eprint("ERROR: No Flux Kustomization spec.path roots found. Provide --roots or ensure Flux Kustomization CRs exist in repo.")
            return 2

    # reachable set
    reachable, missing_refs, invalid_dir_refs, walk_errors = collect_reachable_files(repo, root_dirs, ignore_globs)
    reachable_set = {p.resolve() for p in reachable}

    # candidates to evaluate for orphan (optionally only changed files)
    try:
        eval_candidates = list_yaml_candidates(repo, ignore_globs, use_git=args.use_git, changed_since=args.changed_since)
    except Exception as ex:
        eprint(f"ERROR: unable to list YAML candidates for evaluation: {ex}")
        return 2

    orphan_files: List[Path] = []
    orphan_kustomizations: List[Path] = []

    for f in eval_candidates:
        # Flux control-plane YAMLs are entrypoints, not workload manifests.
        if is_flux_control_plane_only_yaml(f):
            continue

        if f not in reachable_set:
            orphan_files.append(f)
            if f.name in KUSTOMIZATION_FILENAMES:
                orphan_kustomizations.append(f)

    report = Report(
        roots=[rel(repo, r) for r in root_dirs],
        reachable_files=sorted({rel(repo, p) for p in reachable_set}),
        orphan_files=sorted({rel(repo, p) for p in orphan_files}),
        orphan_kustomizations=sorted({rel(repo, p) for p in orphan_kustomizations}),
        missing_refs=missing_refs,
        invalid_dir_refs=invalid_dir_refs,
        mixed_flux_and_workload_files=sorted(set(mixed_files)),
        errors=walk_errors,
    )

    # output
    if args.json_out:
        print(json.dumps(report.__dict__, indent=2, sort_keys=True))
    else:
        print("== Repo Orphan Check (Workload manifests reachable from Flux roots) ==")
        print(f"Repo: {repo}")
        print(f"Roots ({len(report.roots)}):")
        for r in report.roots:
            print(f"  - {r}")

        if report.mixed_flux_and_workload_files:
            print("\nERROR: YAML files mixing Flux control-plane objects and workload objects (split them):")
            for p in report.mixed_flux_and_workload_files[:200]:
                print(f"  - {p}")
            if len(report.mixed_flux_and_workload_files) > 200:
                print(f"  ... ({len(report.mixed_flux_and_workload_files)-200} more)")

        if report.errors:
            print("\nErrors while walking kustomize graph:")
            for e in report.errors:
                print(f"  - {e}")

        if report.missing_refs:
            print("\nMissing referenced files:")
            for m in report.missing_refs[:200]:
                print(f"  - from={m['from']} kind={m['kind']} ref={m['ref']} reason={m['reason']}")
            if len(report.missing_refs) > 200:
                print(f"  ... ({len(report.missing_refs)-200} more)")

        if report.invalid_dir_refs:
            print("\nInvalid directory references (dir referenced but no kustomization.yaml/yml/Kustomization inside):")
            for m in report.invalid_dir_refs[:200]:
                print(f"  - dir={m['dir']} reason={m['reason']}")
            if len(report.invalid_dir_refs) > 200:
                print(f"  ... ({len(report.invalid_dir_refs)-200} more)")

        print(f"\nReachable files: {len(report.reachable_files)}")
        print(f"Orphan workload YAML files: {len(report.orphan_files)}")

        if report.orphan_files:
            print("\nOrphan workload YAML files (unreachable from roots):")
            for o in report.orphan_files[:500]:
                print(f"  - {o}")
            if len(report.orphan_files) > 500:
                print(f"  ... ({len(report.orphan_files)-500} more)")

    # exit code rules (production-friendly)
    hard_errors = (
        bool(report.errors)
        or bool(report.missing_refs)
        or bool(report.invalid_dir_refs)
        or bool(report.mixed_flux_and_workload_files)
    )
    if hard_errors:
        return 2

    if report.orphan_files and args.mode == "fail":
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
