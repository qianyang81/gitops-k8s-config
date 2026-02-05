#!/usr/bin/env python3
"""
Validate Flux Kustomizations in topological order:

- Reads a plan.json produced by build-dag.py (list of nodes with id/name/namespace/path/file_source/etc.)
- For each node:
  1) flux build kustomization ... --dry-run  -> writes rendered manifests to a file
  2) kubeconform ...                          -> validates rendered file against schemas
- Prints a consolidated report, including "possibly problematic files" (best-effort)
- Exits non-zero if any node fails to build or validate

Requirements:
- flux CLI installed (flux build kustomization ...)
- kubeconform installed
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


YAML_REF_RE = re.compile(
    r"""(?P<path>
        (?:[A-Za-z]:[\\/])?          # Windows drive (optional)
        [^\s'"]+?\.(?:ya?ml)         # *.yaml/*.yml
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def sanitize_filename(s: str) -> str:
    # Keep it readable and filesystem-friendly
    s = s.replace("/", "__")
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s)
    return s[:200]


def load_plan(plan_path: Path) -> List[Dict[str, Any]]:
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"plan.json must be a list, got {type(data)}")
    return data


def run_cmd(
    cmd: List[str],
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout_sec: int = 600,
) -> Tuple[int, str, str]:
    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_sec,
    )
    return p.returncode, p.stdout, p.stderr

def extract_yaml_refs(text: str, repo_root: Path) -> Set[str]:
    """
    Best-effort: scan stdout/stderr for *.yaml/*.yml paths.
    Preference: keep ONLY repo-relative paths when the resolved file is inside repo_root.
    Otherwise: ignore (do not keep absolute/outside paths).
    """
    refs: Set[str] = set()
    for m in YAML_REF_RE.finditer(text or ""):
        raw = m.group("path")
        raw = raw.strip(" ,;:()[]{}<>")
        if not raw:
            continue

        try:
            p = Path(raw)

            # Resolve to an absolute path for containment check
            if p.is_absolute():
                abs_p = p.resolve()
            else:
                abs_p = (repo_root / p).resolve()

            # Keep ONLY repo-relative paths
            rel = abs_p.relative_to(repo_root)  # raises if outside repo
            # Normalize: remove leading "./" that might survive in some cases
            rel_str = str(rel)
            if rel_str.startswith("./"):
                rel_str = rel_str[2:]
            refs.add(rel_str)

        except Exception:
            # If we can't make it repo-relative, we skip it (per your preference)
            continue

    return refs


@dataclasses.dataclass(frozen=True)
class Node:
    node_id: str
    name: str
    namespace: str
    path: Optional[str]
    file_source: Optional[str]
    topo_index: Optional[int]

    @staticmethod
    def from_plan_item(item: Dict[str, Any]) -> "Node":
        node_id = str(item.get("id", ""))
        name = str(item.get("name", "")) or node_id.split("/", 1)[-1]
        namespace = str(item.get("namespace", "")) or node_id.split("/", 1)[0]
        path = item.get("path")
        file_source = item.get("file_source")
        topo_index = item.get("topo_index")
        return Node(
            node_id=node_id,
            name=name,
            namespace=namespace,
            path=str(path) if path is not None else None,
            file_source=str(file_source) if file_source is not None else None,
            topo_index=int(topo_index) if topo_index is not None else None,
        )


@dataclasses.dataclass
class NodeResult:
    node: Node
    flux_rc: int
    flux_stdout: str
    flux_stderr: str
    rendered_file: Optional[Path]

    kubeconform_rc: Optional[int] = None
    kubeconform_stdout: str = ""
    kubeconform_stderr: str = ""
    kubeconform_json: Optional[Dict[str, Any]] = None

    possible_problem_files: Set[str] = dataclasses.field(default_factory=set)

    def ok(self) -> bool:
        if self.flux_rc != 0:
            return False
        if self.kubeconform_rc is None:
            return False
        return self.kubeconform_rc == 0


def flux_build(
    node: Node,
    repo_root: Path,
    out_dir: Path,
    flux_bin: str,
    timeout_sec: int,
) -> NodeResult:
    safe_mkdir(out_dir)

    # Determine local manifests path for flux build
    # Flux docs: flux build kustomization NAME --path <local/manifests> [--kustomization-file <flux-ks.yaml>] --dry-run
    # :contentReference[oaicite:2]{index=2}
    if not node.path:
        # If spec.path missing, we still try "." relative to repo root
        local_path = repo_root
    else:
        local_path = (repo_root / node.path).resolve()

    rendered_name = sanitize_filename(f"{node.topo_index or 0:04d}__{node.namespace}__{node.name}__{node.node_id}")
    rendered_file = out_dir / f"{rendered_name}.rendered.yaml"
    stderr_file = out_dir / f"{rendered_name}.flux.stderr.txt"

    cmd = [
        flux_bin,
        "build",
        "kustomization",
        node.name,
        "-n", node.namespace,
        "--path", str(local_path),
        "--dry-run",
    ]
    if node.file_source:
        ks_file = (repo_root / node.file_source).resolve()
        cmd += ["--kustomization-file", str(ks_file)]

    rc, out, err = run_cmd(cmd, cwd=repo_root, timeout_sec=timeout_sec)

    # Persist logs/artifacts
    if out:
        rendered_file.write_text(out, encoding="utf-8")
    if err:
        stderr_file.write_text(err, encoding="utf-8")

    r = NodeResult(
        node=node,
        flux_rc=rc,
        flux_stdout=out,
        flux_stderr=err,
        rendered_file=rendered_file if out else None,
    )

    # Collect "possible problem files" (best-effort)
    if node.file_source:
        r.possible_problem_files.add(node.file_source)
    if node.path:
        r.possible_problem_files.add(node.path.rstrip("/") + "/")
    r.possible_problem_files |= extract_yaml_refs(out, repo_root)
    r.possible_problem_files |= extract_yaml_refs(err, repo_root)
    if err:
        # also include our saved stderr file
        r.possible_problem_files.add(str(stderr_file.relative_to(repo_root)) if stderr_file.is_relative_to(repo_root) else str(stderr_file))

    return r


def kubeconform_validate(
    r: NodeResult,
    repo_root: Path,
    kubeconform_bin: str,
    kubernetes_version: str,
    strict: bool,
    ignore_missing_schemas: bool,
    schema_locations: List[str],
    skip_kinds: List[str],
    reject_kinds: List[str],
    timeout_sec: int,
) -> NodeResult:
    if r.flux_rc != 0 or not r.rendered_file or not r.rendered_file.exists():
        # no-op
        return r

    cmd = [
        kubeconform_bin,
        "-summary",
        "-output",
        "json",
        "-kubernetes-version",
        kubernetes_version,
    ]
    if strict:
        cmd.append("-strict")
    if ignore_missing_schemas:
        cmd.append("-ignore-missing-schemas")

    # multiple -schema-location
    for sl in schema_locations:
        cmd += ["-schema-location", sl]

    if skip_kinds:
        cmd += ["-skip", ",".join(skip_kinds)]
    if reject_kinds:
        cmd += ["-reject", ",".join(reject_kinds)]

    cmd.append(str(r.rendered_file))

    rc, out, err = run_cmd(cmd, cwd=repo_root, timeout_sec=timeout_sec)
    r.kubeconform_rc = rc
    r.kubeconform_stdout = out
    r.kubeconform_stderr = err

    # parse json output (best-effort)
    try:
        r.kubeconform_json = json.loads(out) if out.strip() else None
    except Exception:
        r.kubeconform_json = None

    # --- Write simple error text file (filename + status + msg only) ---
    if r.rendered_file and isinstance(r.kubeconform_json, dict):
        resources = r.kubeconform_json.get("resources", [])
        lines: List[str] = []

        if isinstance(resources, list):
            for it in resources:
                if not isinstance(it, dict):
                    continue

                status = str(it.get("status", "")).strip()
                status_lc = status.lower()

                # Skip only clearly valid results
                if status_lc in ("valid", "success"):
                    continue

                filename = str(it.get("filename", "")).strip()
                msg = str(it.get("msg", "")).strip()

                if not filename:
                    continue

                if msg:
                    lines.append(f"{filename}: {status} - {msg}")
                else:
                    lines.append(f"{filename}: {status}")

        if lines:
            base = r.rendered_file.with_suffix("")
            err_file = Path(str(base) + ".kubeconform.errors.txt")
            err_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    r.possible_problem_files |= extract_yaml_refs(out, repo_root)
    r.possible_problem_files |= extract_yaml_refs(err, repo_root)

    return r

def require_bin(name: str) -> str:
    p = shutil.which(name)
    if not p:
        raise FileNotFoundError(f"Required binary not found in PATH: {name}")
    return p


def print_report(results: List[NodeResult], repo_root: Path, show_ok: bool) -> int:
    bad_nodes = [x for x in results if not x.ok()]
    all_problem_files: Set[str] = set()

    print("\n==================== Flux Topology Build + Kubeconform Report ====================")
    print(f"Nodes: {len(results)} | OK: {len(results)-len(bad_nodes)} | BAD: {len(bad_nodes)}")

    for r in results:
        if (not show_ok) and r.ok():
            continue

        status = "OK" if r.ok() else "BAD"
        idx = r.node.topo_index if r.node.topo_index is not None else -1
        print("\n----------------------------------------------------------------------------------")
        print(f"[{status}] #{idx} {r.node.node_id} (name={r.node.name}, ns={r.node.namespace})")
        if r.node.file_source:
            print(f"  - kustomization CR file : {r.node.file_source}")
        if r.node.path:
            print(f"  - spec.path             : {r.node.path}")
        print(f"  - flux rc               : {r.flux_rc}")
        if r.rendered_file:
            try:
                rel = r.rendered_file.relative_to(repo_root)
                print(f"  - rendered output       : {rel}")
            except Exception:
                print(f"  - rendered output       : {r.rendered_file}")
        if r.kubeconform_rc is not None:
            print(f"  - kubeconform rc        : {r.kubeconform_rc}")

        if r.flux_rc != 0:
            print("  Flux error (first ~40 lines):")
            lines = (r.flux_stderr or "").splitlines()
            for ln in lines[:40]:
                print(f"    {ln}")

        if r.kubeconform_rc not in (None, 0):
            # Show a compact summary
            if r.kubeconform_json and isinstance(r.kubeconform_json.get("summary"), dict):
                s = r.kubeconform_json["summary"]
                print(f"  Kubeconform summary: valid={s.get('valid')} invalid={s.get('invalid')} errors={s.get('errors')} skipped={s.get('skipped')}")

                bad = [x for x in r.kubeconform_json.get('resources') if x.get("status") != "valid"]
                if bad:
                    print("  Kubeconform errors (first ~10):")
                    for it in bad[:10]:
                        msg = it.get("msg", "").strip()
                        if msg:
                            print(f"    - filename: {it.get('filename','?')}")
                            print(f"    - status: {it.get('status')}")
                            print(f"    - message: {msg}")

            else:
                print("  Kubeconform output (first ~40 lines):")
                lines = (r.kubeconform_stdout or r.kubeconform_stderr or "").splitlines()
                for ln in lines[:40]:
                    print(f"    {ln}")

        # accumulate files
        all_problem_files |= set(r.possible_problem_files)

        if not r.ok():
            # for BAD nodes, print a focused list
            print("  Possible problematic files/paths (best-effort):")
            for f in sorted(r.possible_problem_files):
                print(f"    - {f}")

    print("\n==================== Possibly problematic files (union) ====================")
    for f in sorted(all_problem_files):
        print(f"- {f}")

    return 1 if bad_nodes else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Flux Kustomizations from plan.json and validate with kubeconform")
    ap.add_argument("--root", default=".", help="Repo root (same as build-dag.py --root)")
    ap.add_argument("--plan", default="out/plan.json", help="Topology plan.json path (relative to --root)")
    ap.add_argument("--artifacts-dir", default="out/rendered", help="Where to store rendered YAML + logs (relative to --root)")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) // 2), help="Parallel jobs")
    ap.add_argument("--timeout-sec", type=int, default=600, help="Per-command timeout seconds")

    # kubeconform options
    ap.add_argument("--kubernetes-version", default="master", help="kubeconform -kubernetes-version (e.g. 1.30.0)")
    ap.add_argument("--strict", action="store_true", help="kubeconform -strict")
    ap.add_argument("--ignore-missing-schemas", action="store_true", help="kubeconform -ignore-missing-schemas")
    ap.add_argument(
        "--schema-location",
        action="append",
        default=["default"],
        help="kubeconform -schema-location (repeatable). default is 'default'",
    )
    ap.add_argument("--skip-kind", action="append", default=[], help="kubeconform -skip (repeatable, will be merged)")
    ap.add_argument("--reject-kind", action="append", default=[], help="kubeconform -reject (repeatable, will be merged)")

    ap.add_argument("--show-ok", action="store_true", help="Also print OK nodes (default prints only BAD nodes)")
    args = ap.parse_args()

    repo_root = Path(args.root).resolve()
    plan_path = (repo_root / args.plan).resolve()
    artifacts_dir = (repo_root / args.artifacts_dir).resolve()

    if not plan_path.exists():
        eprint(f"plan.json not found: {plan_path}")
        return 2

    flux_bin = require_bin("flux")
    kubeconform_bin = require_bin("kubeconform")

    plan_items = load_plan(plan_path)
    nodes = [Node.from_plan_item(x) for x in plan_items]

    # Keep order stable for reporting, but execute build+validate concurrently per node.
    # (We don't enforce dependency execution here, because plan.json already gives topo order;
    #  concurrency is safe for pure local builds.)
    def worker(n: Node) -> NodeResult:
        r = flux_build(n, repo_root, artifacts_dir, flux_bin, args.timeout_sec)
        r = kubeconform_validate(
            r,
            repo_root,
            kubeconform_bin,
            kubernetes_version=args.kubernetes_version,
            strict=args.strict,
            ignore_missing_schemas=args.ignore_missing_schemas,
            schema_locations=list(args.schema_location or ["default"]),
            skip_kinds=list(args.skip_kind or []),
            reject_kinds=list(args.reject_kind or []),
            timeout_sec=args.timeout_sec,
        )
        # Always include rendered output path if exists
        if r.rendered_file and r.rendered_file.exists():
            try:
                r.possible_problem_files.add(str(r.rendered_file.relative_to(repo_root)))
            except Exception:
                r.possible_problem_files.add(str(r.rendered_file))
        return r

    results: List[NodeResult] = []
    with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        futs = [ex.submit(worker, n) for n in nodes]
        for f in cf.as_completed(futs):
            results.append(f.result())

    # Sort back by topo_index then id
    results.sort(key=lambda x: ((x.node.topo_index or 10**9), x.node.node_id))

    return print_report(results, repo_root, show_ok=args.show_ok)


if __name__ == "__main__":
    raise SystemExit(main())
