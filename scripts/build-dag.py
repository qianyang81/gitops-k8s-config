#!/usr/bin/env python3
import os
import json
import yaml
import argparse
from pathlib import Path
from typing import Dict, Any, List, Iterable

import networkx as nx


FLUX_KUSTOMIZE_API_PREFIX = "kustomize.toolkit.fluxcd.io/"


def _is_yaml_file(p: Path) -> bool:
    return p.suffix in [".yaml", ".yml"]


def _rel(repo_root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(repo_root))
    except Exception:
        return str(p)


def _safe_load_all(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for doc in yaml.safe_load_all(f):
            if isinstance(doc, dict) and doc:
                yield doc


class FluxDAGBuilder:
    def __init__(
        self,
        repo_root: str,
        default_namespace: str = "flux-system",
        allow_external_deps: bool = False,
        strict_flux_only: bool = True,
        max_cycles: int = 50,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.default_namespace = default_namespace
        self.allow_external_deps = allow_external_deps
        self.strict_flux_only = strict_flux_only
        self.max_cycles = max_cycles

        self.dag = nx.DiGraph()
        self.nodes_data: Dict[str, Dict[str, Any]] = {}

    def parse_kustomizations(self, search_paths: List[str]) -> None:
        """
        Recursively scan for Flux Kustomization CRs under search_paths (relative to repo_root).

        Production behavior:
          - Collect all parsing errors and report them together (compiler-style).
          - Detect duplicates for <namespace>/<name>.
        """
        errors: List[str] = []
        visited_files: List[Path] = []

        for sp in search_paths:
            scan_root = (self.repo_root / sp).resolve()
            if not scan_root.exists():
                errors.append(f"Search path not found: {scan_root}")
                continue

            for root, _, files in os.walk(scan_root):
                for fname in files:
                    fp = Path(root) / fname
                    if not _is_yaml_file(fp):
                        continue

                    visited_files.append(fp)

                    try:
                        for doc in _safe_load_all(fp):
                            kind = doc.get("kind")
                            api = doc.get("apiVersion", "")

                            if kind != "Kustomization":
                                continue

                            # Avoid mixing with kustomize.config.k8s.io Kustomization
                            if self.strict_flux_only and not str(api).startswith(FLUX_KUSTOMIZE_API_PREFIX):
                                continue

                            md = doc.get("metadata") or {}
                            name = md.get("name")
                            if not name:
                                raise ValueError(f"Missing metadata.name in {_rel(self.repo_root, fp)}")

                            namespace = md.get("namespace") or self.default_namespace
                            spec = doc.get("spec") or {}
                            node_id = f"{namespace}/{name}"

                            # detect duplicates
                            if node_id in self.nodes_data:
                                prev = self.nodes_data[node_id].get("file_source")
                                raise ValueError(
                                    f"Duplicate Flux Kustomization definition for {node_id}\n"
                                    f"  - first : {prev}\n"
                                    f"  - second: {_rel(self.repo_root, fp)}"
                                )

                            # dependsOn supports namespace per item (default to current ns)
                            deps: List[str] = []
                            raw_depends = spec.get("dependsOn") or []
                            if not isinstance(raw_depends, list):
                                raise ValueError(
                                    f"spec.dependsOn must be a list in {_rel(self.repo_root, fp)} ({node_id})"
                                )

                            for d in raw_depends:
                                if not isinstance(d, dict) or "name" not in d:
                                    raise ValueError(
                                        f"Invalid dependsOn entry in {_rel(self.repo_root, fp)} ({node_id}): {d}"
                                    )
                                dep_name = d["name"]
                                dep_ns = d.get("namespace", namespace)
                                deps.append(f"{dep_ns}/{dep_name}")

                            path = spec.get("path")
                            rel_file = _rel(self.repo_root, fp)

                            self.nodes_data[node_id] = {
                                "id": node_id,
                                "name": str(name),
                                "namespace": str(namespace),
                                "apiVersion": str(api),
                                "path": path,
                                "dependsOn": deps,
                                "file_source": rel_file,
                            }
                            self.dag.add_node(node_id)

                    except Exception as e:
                        errors.append(f"Failed to parse {_rel(self.repo_root, fp)}: {e}")

        if errors:
            msg = "🚨 Errors while scanning/parsing Flux Kustomizations:\n" + "\n".join([f"- {x}" for x in errors])
            raise RuntimeError(msg)

        if not visited_files:
            raise RuntimeError("No YAML files found under search paths: " + ", ".join(search_paths))

        if not self.nodes_data:
            raise RuntimeError(
                "No Flux Kustomization objects found. "
                "Check --search paths or strict_flux_only/apiVersion filter."
            )

    def build_graph(self) -> None:
        """
        Add edges based on dependsOn, validate missing deps and cycles.
        Edge direction: dep -> node (execution order).
        """
        missing: List[str] = []

        for node_id, data in self.nodes_data.items():
            for dep_id in data["dependsOn"]:
                if dep_id not in self.nodes_data:
                    src = data.get("file_source", "unknown")
                    msg = (
                        f"{node_id} depends on {dep_id}, but it was not found in scanned paths.\n"
                        f"    at: {src}"
                    )
                    if self.allow_external_deps:
                        print(f"💡 Info: {msg}")
                        # keep dep node for visualization honesty
                        if dep_id not in self.dag:
                            self.dag.add_node(dep_id)
                        continue

                    missing.append(msg)
                    continue

                self.dag.add_edge(dep_id, node_id)

        if missing:
            raise ValueError("🚨 Missing dependencies:\n" + "\n".join([f"- {m}" for m in missing]))

        if not nx.is_directed_acyclic_graph(self.dag):
            cycles: List[List[str]] = []
            for i, c in enumerate(nx.simple_cycles(self.dag), start=1):
                if i > self.max_cycles:
                    cycles.append(["... (more cycles omitted) ..."])
                    break
                cycles.append(c + [c[0]] if c else c)

            formatted = "\n".join(["- " + " -> ".join(c) for c in cycles if c])
            raise ValueError("🚨 Circular dependencies detected (graph is NOT a DAG):\n" + formatted)

    def export_json(self, output_file: str) -> None:
        """
        Export topologically sorted plan.json for subsequent build/validation pipeline.
        """
        ordered_nodes = list(nx.topological_sort(self.dag))
        final_output: List[Dict[str, Any]] = []
        for idx, node in enumerate(ordered_nodes, start=1):
            item = dict(self.nodes_data.get(node, {"id": node}))
            item["topo_index"] = idx
            final_output.append(item)

        out = (self.repo_root / output_file).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(final_output, indent=2), encoding="utf-8")
        print(f"📦 Topology JSON exported: {out}")

    def export_mermaid(self, output_file: str) -> None:
        """
        Export Mermaid graph for GitHub rendering.
        """
        lines = ["graph TD"]

        def mid(node_id: str) -> str:
            return "n_" + node_id.replace("/", "__").replace("-", "_").replace(".", "_")

        for node in self.dag.nodes():
            nid = mid(node)
            lines.append(f'    {nid}["{node}"]')

        for u, v in self.dag.edges():
            lines.append(f"    {mid(u)} --> {mid(v)}")

        out = (self.repo_root / output_file).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"📊 Mermaid graph exported: {out}")

    def export_dot(self, output_file: str) -> None:
        """
        Export Graphviz dot file (requires pydot).
        Keeps readable labels as 'namespace/name'.
        """
        try:
            from networkx.drawing.nx_pydot import to_pydot
        except ImportError:
            print("⚠️ Warning: pydot not installed. Skipping .dot export. (pip install pydot)")
            return

        def did(node_id: str) -> str:
            return node_id.replace("/", "__").replace("-", "_").replace(".", "_")

        mapping = {n: did(n) for n in self.dag.nodes()}
        dot_dag = nx.relabel_nodes(self.dag, mapping)

        out = (self.repo_root / output_file).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)

        pd = to_pydot(dot_dag)
        for orig, nid in mapping.items():
            node = pd.get_node(nid)
            if node:
                node[0].set_label(orig)

        out.write_text(pd.to_string(), encoding="utf-8")
        print(f"📐 Graphviz dot exported: {out}")


def main():
    parser = argparse.ArgumentParser(description="Flux CD Dependency DAG Builder (plan + graph)")
    parser.add_argument("--root", default=".", help="Git repository root directory")
    parser.add_argument(
        "--search",
        nargs="+",
        default=["clusters"],
        help="One or more directories to scan (relative to root). e.g. --search clusters apps infrastructure",
    )
    parser.add_argument(
        "--default-namespace",
        default="flux-system",
        help="Default namespace if metadata.namespace missing",
    )
    parser.add_argument(
        "--allow-external-deps",
        action="store_true",
        help="Allow dependsOn pointing outside scanned set (still visualized as nodes).",
    )
    parser.add_argument("--out-dir", default="out", help="Output directory (relative to root)")
    parser.add_argument("--dot", action="store_true", help="Also export graph.dot (requires pydot)")
    parser.add_argument("--max-cycles", type=int, default=50, help="Max cycles to print when cycles exist")
    args = parser.parse_args()

    out_dir = args.out_dir.rstrip("/")

    builder = FluxDAGBuilder(
        repo_root=args.root,
        default_namespace=args.default_namespace,
        allow_external_deps=args.allow_external_deps,
        strict_flux_only=True,
        max_cycles=args.max_cycles,
    )

    builder.parse_kustomizations(args.search)
    builder.build_graph()

    builder.export_json(f"{out_dir}/plan.json")
    builder.export_mermaid(f"{out_dir}/graph.mmd")
    if args.dot:
        builder.export_dot(f"{out_dir}/graph.dot")


if __name__ == "__main__":
    main()
