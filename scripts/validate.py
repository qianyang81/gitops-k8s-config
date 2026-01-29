#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, List, Set, Tuple, Dict


# ---------- Colors ----------
RED = "\033[0;31m"
GREEN = "\033[0;32m"
BLUE = "\033[1;34m"
NC = "\033[0m"


def log(msg: str) -> None:
    print(f"{BLUE}==> {msg}{NC}")


def ok(msg: str) -> None:
    print(f"{GREEN}✔ {msg}{NC}")


def fail(msg: str, code: int = 1) -> None:
    print(f"{RED}✖ {msg}{NC}", file=sys.stderr)
    sys.exit(code)


def run(cmd: List[str], *, capture: bool = False, text: bool = True, check: bool = False, stdin_data: str | None = None) -> subprocess.CompletedProcess:
    """
    Wrapper around subprocess.run with good defaults.
    """
    if capture:
        return subprocess.run(
            cmd,
            input=stdin_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            check=check,
        )
    return subprocess.run(cmd, input=stdin_data, text=text, check=check)


def which_or_fail(cmd: str) -> None:
    if shutil_which(cmd) is None:
        fail(f"Missing tool: {cmd}")


def shutil_which(cmd: str) -> str | None:
    # tiny replacement to avoid importing shutil for one call
    for p in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(p) / cmd
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def has_kustomization_file(dirpath: Path) -> bool:
    return any((dirpath / f).is_file() for f in ["kustomization.yaml", "kustomization.yml", "Kustomization"])


# ---------- YAML resource ID extraction ----------
# Output format (tab-separated):
# apiVersion  kind  namespace  name
YQ_EXTRACT_IDS = r"""
select(type == "!!map")
| select(has("apiVersion") and has("kind") and has("metadata"))
| select(.metadata.name != null)
| [
    .apiVersion,
    .kind,
    (.metadata.namespace // "default"),
    .metadata.name
  ]
| @tsv
""".strip()


def yq_extract_ids_from_file(path: Path) -> List[str]:
    """
    Returns list of lines: 'apiVersion<TAB>kind<TAB>namespace<TAB>name'
    """
    cp = run(["yq", "eval", "-r", YQ_EXTRACT_IDS, str(path)], capture=True)
    if cp.returncode != 0:
        # Not fatal; some files may not be parseable as YAML docs in yq for various reasons
        return []
    lines = [ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]
    return lines


# ---------- kustomize build + kubeconform ----------
def kustomize_build(target: Path) -> Tuple[int, str, str]:
    cp = run(["kustomize", "build", str(target)], capture=True)
    return cp.returncode, cp.stdout, cp.stderr


def kubeconform_validate(manifest_yaml: str) -> Tuple[int, str, str]:
    cp = run(["kubeconform", "-strict", "-ignore-missing-schemas"], capture=True, stdin_data=manifest_yaml)
    return cp.returncode, cp.stdout, cp.stderr


def run_kustomize_build_validate_and_collect_ids(target: Path, built_ids: Set[str]) -> None:
    log(f"Kustomize build ({target})...")
    rc, out, err = kustomize_build(target)
    if rc != 0:
        print("\n----- kustomize error output ({}) -----".format(target), file=sys.stderr)
        # kustomize tends to write errors to stderr; but sometimes on stdout.
        print((err or out).rstrip(), file=sys.stderr)
        print("-------------------------------------------\n", file=sys.stderr)
        fail(f"Kustomize build failed: {target}")

    ok(f"{target} build OK")

    log(f"Schema validation (kubeconform) ({target})...")
    vrc, vout, verr = kubeconform_validate(out)
    if vrc != 0:
        print("\n----- kubeconform error output ({}) -----".format(target), file=sys.stderr)
        print((verr or vout).rstrip(), file=sys.stderr)
        print("------------------------------------------------\n", file=sys.stderr)
        fail(f"Schema validation failed: {target}")
    ok(f"{target} schema OK")

    # collect resource IDs from built output using yq (stdin)
    # This keeps behavior consistent with your previous script.
    ids = yq_extract_ids_from_stream(out)
    built_ids.update(ids)


def yq_extract_ids_from_stream(yaml_text: str) -> Set[str]:
    cp = run(["yq", "eval", "-r", YQ_EXTRACT_IDS, "-"], capture=True, stdin_data=yaml_text)
    if cp.returncode != 0:
        return set()
    return {ln.strip() for ln in cp.stdout.splitlines() if ln.strip()}


# ---------- Flux spec.path extraction ----------
YQ_EXTRACT_FLUX_PATHS = r'''
select(
  type == "!!map"
  and .kind == "Kustomization"
  and (.apiVersion | test("^kustomize\\.toolkit\\.fluxcd\\.io/"))
)
| .spec.path
| select(type == "!!str" and . != "")
'''





def extract_flux_kustomization_paths(root: Path) -> List[Path]:
    """
    Find YAML under 'clusters' and extract Flux Kustomization .spec.path.
    """
    clusters_dir = root / "clusters/aks/root"
    if not clusters_dir.exists():
        return []

    yaml_files = [p for p in clusters_dir.rglob("*") if p.is_file() and p.suffix in [".yaml", ".yml"]]
    if not yaml_files:
        return []

    # Use yq over all files at once (fast, consistent)
    cmd = ["yq", "eval-all", "--no-doc", "-r", YQ_EXTRACT_FLUX_PATHS] + [str(p) for p in yaml_files]
    cp = run(cmd, capture=True)
    if cp.returncode != 0:
        # if yq returns error (some files), we still try best-effort: run file by file
        paths: Set[str] = set()
        for f in yaml_files:
            cpi = run(["yq", "eval", "-r", YQ_EXTRACT_FLUX_PATHS, str(f)], capture=True)
            if cpi.returncode == 0:
                for ln in cpi.stdout.splitlines():
                    ln = ln.strip()
                    if ln:
                        paths.add(ln)
        return normalize_paths(paths)

    paths = {ln.strip() for ln in cp.stdout.splitlines() if ln.strip()}
    return normalize_paths(paths)


def normalize_paths(paths: Iterable[str]) -> List[Path]:
    normed = set()
    for p in paths:
        p = p.strip()
        if p.startswith("./"):
            p = p[2:]
        p = p.rstrip("/")
        if p:
            normed.add(p)
    return [Path(p) for p in sorted(normed)]


# ---------- Repo scanning (Method A) ----------
def find_repo_yaml_files(root: Path, excludes: List[str]) -> List[Path]:
    """
    Return yaml/yml files under root, excluding patterns and kustomization files.
    """
    out: List[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix not in [".yaml", ".yml"]:
            continue

        name = p.name
        if name in ("kustomization.yaml", "kustomization.yml", "Kustomization"):
            continue

        rel = str(p.relative_to(root)).replace("\\", "/")
        
        if "/patches/" in f"/{rel}/":
            continue
        
        if any(rel.startswith(ex.rstrip("/") + "/") or rel == ex.rstrip("/") for ex in excludes):
            continue

        out.append(p)
    return out


def build_repo_id_map(yaml_files: List[Path]) -> Tuple[Set[str], Dict[str, List[Path]]]:
    """
    Returns:
      - repo_ids_only: set of resource IDs
      - repo_ids_to_files: map id -> [files...]
    """
    repo_ids_only: Set[str] = set()
    repo_ids_to_files: Dict[str, List[Path]] = {}

    for f in yaml_files:
        ids = yq_extract_ids_from_file(f)
        for rid in ids:
            repo_ids_only.add(rid)
            repo_ids_to_files.setdefault(rid, []).append(f)

    return repo_ids_only, repo_ids_to_files


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate GitOps repo (kustomize + flux paths + schema + unreferenced resource detection).")
    parser.add_argument("--root", default=".", help="Repo root (default: .)")
    parser.add_argument("--skip-yamllint", action="store_true", help="Skip yamllint")
    parser.add_argument("--skip-unreferenced-check", action="store_true", help="Skip Method A unreferenced resource detection")
    parser.add_argument("--exclude", action="append", default=[], help="Exclude path prefix from repo scan (repeatable), e.g. --exclude docs --exclude examples")
    args = parser.parse_args()

    root = Path(args.root).resolve()

    # ---------- Tool check ----------
    log("Checking required tools...")
    required = ["kustomize", "kubeconform", "yq"]
    if not args.skip_yamllint:
        required.append("yamllint")

    for cmd in required:
        if shutil_which(cmd) is None:
            fail(f"Missing tool: {cmd}")
    ok("All tools present")

    # ---------- YAML lint ----------
    if not args.skip_yamllint:
        log("YAML lint...")
        cp = run(["yamllint", str(root)], capture=True)
        if cp.returncode != 0:
            print(cp.stdout, end="")
            print(cp.stderr, end="", file=sys.stderr)
            fail("YAML issues found (see above)")
        ok("YAML syntax OK")

    # ---------- Kustomize build and schema validation of entry targets ----------
    built_ids: Set[str] = set()
    entry_targets: List[Path] = [
        root / "clusters/aks/root",
        root / "clusters/aks/env/dev",
        root / "clusters/aks/env/staging",
        root / "clusters/aks/env/prod",
    ]

    for t in entry_targets:
        if not t.exists():
            fail(f"Entry target not found: {t}")
        if not has_kustomization_file(t):
            fail(f"No kustomization.yaml/yml/Kustomization found in: {t}")
        run_kustomize_build_validate_and_collect_ids(t, built_ids)

    # ---------- Flux spec.path targets ----------
    log("Extracting Flux Kustomization spec.path entries...")
    flux_paths = extract_flux_kustomization_paths(root)
    if not flux_paths:
        log(f"No Flux Kustomization CRs found under clusters/aks/root (nothing to validate via spec.path).")
    else:
        ok(f"Found {len(flux_paths)} Flux spec.path entries")

    for rel in flux_paths:
        p = (root / rel).resolve()
        if not p.exists() or not p.is_dir():
            fail(f"Flux spec.path directory not found: {rel}")
        if not has_kustomization_file(p):
            fail(f"No kustomization.yaml/yml/Kustomization found in: {rel}")
        run_kustomize_build_validate_and_collect_ids(p, built_ids)

    # ---------- Check unreferenced resources ----------
    if not args.skip_unreferenced_check:
        log("Scanning repository YAML resources (for 'forgot to include' detection)...")
        default_excludes = [".git", ".github", ".terraform", "patches", "apps/**/patches"]
        excludes = default_excludes + list(args.exclude)

        yaml_files = find_repo_yaml_files(root, excludes=excludes)
        if not yaml_files:
            log("No YAML files found to scan.")
        else:
            ok(f"Found {len(yaml_files)} YAML files to scan")

        repo_ids_only, repo_ids_to_files = build_repo_id_map(yaml_files)

        unref = sorted(repo_ids_only - built_ids)
        if not unref:
            ok("No unreferenced resources found (good! Everything appears included by some kustomize entry).")
            ok("All checks passed")
            return 0

        print()
        log(f"Found {len(unref)} unreferenced resource(s) in repo (present in YAML, but not in ANY kustomize build output).")
        print("These are likely: forgotten resources/, missing kustomization.yaml in a folder, or missing 'resources:' reference.")
        print()

        print("----- Unreferenced resources (apiVersion kind ns name) -----")
        for rid in unref:
            print(rid)
        print("-----------------------------------------------------------\n")

        log("Where they come from (file paths):")
        for rid in unref:
            files = repo_ids_to_files.get(rid, [])
            for f in files[:50]:
                print(f"{rid}\t{f.relative_to(root)}")
        print()
        fail("Unreferenced resources detected. Fix by adding missing kustomization.yaml and/or referencing the folder/file from an entry kustomization.")

    ok("All checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
