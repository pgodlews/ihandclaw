"""Dataset builder for merging multiple recorded sessions into a ScreenGraph.

Uses perceptual hashing (dHash/pHash with status-bar masking) to detect
and merge identical screen states across different recording runs, linking
multiple linear sessions into a branching, multi-path screen graph.
Exports structured summaries in Markdown, JSON, and Graphviz DOT formats.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from typing import Any

from PIL import Image

from .graph import ScreenGraph, ScreenNode, Transition
from .hash import compute_dhash, compute_phash, hamming_distance
from .manifest import SessionManifest, parse_bounds


class DatasetBuilder:
    """Merges multiple recorded sessions into a canonical ScreenGraph using perceptual hashing."""

    def __init__(
        self,
        hash_threshold: int = 6,
        hash_method: str = "phash",
    ):
        self.hash_threshold = hash_threshold
        self.hash_method = hash_method

    def _hash_image(self, img: Image.Image) -> str:
        if self.hash_method == "phash":
            return compute_phash(img, mask_status=True)
        return compute_dhash(img, mask_status=True)

    def merge_sessions(
        self,
        session_dirs: list[Path | str],
        output_dir: Path | str | None = None,
    ) -> ScreenGraph:
        """Merge multiple session recordings into a unified ScreenGraph."""
        graph = ScreenGraph()
        canonical_hashes: dict[str, str] = {}  # node_id -> hash
        canonical_images: dict[str, Image.Image] = {}
        node_counter = 0

        out_path = Path(output_dir) if output_dir else None
        if out_path:
            (out_path / "screens").mkdir(parents=True, exist_ok=True)
            (out_path / "elements").mkdir(parents=True, exist_ok=True)

        def get_or_create_canonical_node(
            img_path: Path,
            elements_data: list[dict[str, Any]],
            pointer_data: dict[str, Any] | None = None,
        ) -> ScreenNode:
            nonlocal node_counter
            img = Image.open(img_path).convert("RGB")
            img_hash = self._hash_image(img)

            # Search existing canonical nodes
            best_node_id = None
            min_dist = float("inf")
            for nid, chash in canonical_hashes.items():
                dist = hamming_distance(img_hash, chash)
                if dist <= self.hash_threshold and dist < min_dist:
                    min_dist = dist
                    best_node_id = nid

            if best_node_id is not None:
                # Merge into existing node
                existing_node = graph.nodes[best_node_id]
                # If existing node lacks elements and new has them, enrich
                if not existing_node.elements and elements_data:
                    existing_node.elements = elements_data
                return existing_node

            # Create new canonical node
            node_counter += 1
            node_id = f"screen_{node_counter:03d}_{img_hash[:8]}"
            canonical_hashes[node_id] = img_hash
            canonical_images[node_id] = img

            frame_path_str = str(img_path)
            if out_path:
                dest_frame = out_path / "screens" / f"{node_id}.jpg"
                img.save(dest_frame, format="JPEG", quality=90)
                frame_path_str = f"screens/{node_id}.jpg"

                dest_elem = out_path / "elements" / f"{node_id}.json"
                dest_elem.write_text(json.dumps(elements_data, indent=2))

            new_node = ScreenNode(
                screen_id=node_id,
                frame_path=frame_path_str,
                elements=elements_data,
                pointer=pointer_data,
                metadata={"hash": img_hash, "created_at": datetime.now(timezone.utc).isoformat()},
            )
            graph.add_node(new_node)
            return new_node

        # Process each session manifest
        for sdir_raw in session_dirs:
            sdir = Path(sdir_raw)
            manifest_file = sdir / "manifest.json" if sdir.is_dir() else sdir
            if not manifest_file.exists():
                raise FileNotFoundError(f"Manifest not found at {manifest_file}")

            manifest = SessionManifest.load(manifest_file)
            base_path = manifest_file.parent

            for step in manifest.steps:
                # Load pre-frame and elements
                pre_img_path = base_path / step.pre_frame
                elem_path = base_path / step.elements_file
                elements_raw: list[dict[str, Any]] = []
                pointer_raw: dict[str, Any] | None = None

                if elem_path.exists():
                    try:
                        elem_json = json.loads(elem_path.read_text())
                        if isinstance(elem_json, dict):
                            elements_raw = elem_json.get("elements", [])
                            pointer_raw = elem_json.get("pointer")
                        elif isinstance(elem_json, list):
                            elements_raw = elem_json
                    except Exception:
                        pass

                pre_node = get_or_create_canonical_node(pre_img_path, elements_raw, pointer_raw)

                # Load post-frame
                post_img_path = base_path / step.post_frame
                post_node = get_or_create_canonical_node(post_img_path, [])

                # Find target element for tap action
                action_name = step.action.name if hasattr(step.action, "name") else step.action.get("name", "")
                action_args = step.action.args if hasattr(step.action, "args") else step.action.get("args", {})
                target_elem = None

                if action_name in ("tap", "long_press") and "x" in action_args and "y" in action_args:
                    target_elem = pre_node.find_element_at(float(action_args["x"]), float(action_args["y"]))
                elif action_name == "tap_element" and "id" in action_args:
                    target_elem = pre_node.find_element_by_id(str(action_args["id"]))
                elif action_name == "tap_text" and "text" in action_args:
                    target_elem = pre_node.find_element_by_text(str(action_args["text"]))

                # Check if identical transition already exists
                trans_exists = False
                for t in pre_node.outgoing_transitions:
                    if (
                        t.target_id == post_node.screen_id
                        and t.action_name == action_name
                        and t.action_args == action_args
                    ):
                        trans_exists = True
                        break

                if not trans_exists:
                    trans_id = f"t_{pre_node.screen_id}_{action_name}_{len(pre_node.outgoing_transitions)}"
                    transition = Transition(
                        transition_id=trans_id,
                        source_id=pre_node.screen_id,
                        target_id=post_node.screen_id,
                        action_name=action_name,
                        action_args=action_args,
                        target_element=target_elem,
                        response_result=step.response.get("result", step.response),
                    )
                    graph.add_transition(transition)

        if out_path:
            graph.save(out_path / "graph.json")
            # Save summary files
            summary_json = self.generate_summary(graph)
            (out_path / "summary.json").write_text(json.dumps(summary_json, indent=2))
            summary_md = self.export_summary(graph, format="markdown")
            (out_path / "summary.md").write_text(summary_md)

        return graph

    def generate_summary(self, graph: ScreenGraph) -> dict[str, Any]:
        """Compute statistical and topological summary of the ScreenGraph."""
        total_nodes = len(graph.nodes)
        total_transitions = sum(len(n.outgoing_transitions) for n in graph.nodes.values())

        action_counts: dict[str, int] = {}
        branching_nodes: list[dict[str, Any]] = []
        leaf_nodes: list[str] = []

        for nid, node in graph.nodes.items():
            out_count = len(node.outgoing_transitions)
            if out_count > 1:
                branching_nodes.append({
                    "screen_id": nid,
                    "outgoing_count": out_count,
                    "actions": [t.action_name for t in node.outgoing_transitions],
                })
            elif out_count == 0:
                leaf_nodes.append(nid)

            for t in node.outgoing_transitions:
                action_counts[t.action_name] = action_counts.get(t.action_name, 0) + 1

        return {
            "initial_screen_id": graph.initial_screen_id,
            "total_screens": total_nodes,
            "total_transitions": total_transitions,
            "action_counts": action_counts,
            "branching_screens_count": len(branching_nodes),
            "branching_screens": branching_nodes,
            "leaf_screens_count": len(leaf_nodes),
            "leaf_screens": leaf_nodes,
        }

    def export_summary(self, graph: ScreenGraph, format: str = "markdown") -> str:
        """Export summary report in Markdown, JSON, or Graphviz DOT format."""
        summary = self.generate_summary(graph)

        if format == "json":
            return json.dumps(summary, indent=2)

        if format == "dot":
            lines = ["digraph ScreenGraph {", "  rankdir=LR;", "  node [shape=box, style=rounded];"]
            for nid, node in graph.nodes.items():
                is_init = " (START)" if nid == graph.initial_screen_id else ""
                label = f"{nid}{is_init}\\n{len(node.elements)} elements"
                color = "green" if nid == graph.initial_screen_id else "black"
                lines.append(f'  "{nid}" [label="{label}", color="{color}"];')

                for t in node.outgoing_transitions:
                    arg_summary = ""
                    if "x" in t.action_args and "y" in t.action_args:
                        arg_summary = f"({t.action_args['x']:.0f},{t.action_args['y']:.0f})"
                    elif "combo" in t.action_args:
                        arg_summary = f"'{t.action_args['combo']}'"
                    elif "text" in t.action_args:
                        arg_summary = f"'{t.action_args['text']}'"
                    elif "app" in t.action_args:
                        arg_summary = f"'{t.action_args['app']}'"
                    edge_label = f"{t.action_name} {arg_summary}".strip()
                    lines.append(f'  "{nid}" -> "{t.target_id}" [label="{edge_label}"];')
            lines.append("}")
            return "\n".join(lines)

        # Markdown format
        lines = [
            "# iPhone Hand Replay Simulator - Dataset Summary",
            "",
            f"- **Initial Screen:** `{summary['initial_screen_id']}`",
            f"- **Total Unique Screens:** {summary['total_screens']}",
            f"- **Total Transitions:** {summary['total_transitions']}",
            f"- **Branching Screens:** {summary['branching_screens_count']}",
            f"- **Leaf (Terminal) Screens:** {summary['leaf_screens_count']}",
            "",
            "## Action Distribution",
            "",
            "| Action | Count |",
            "|---|---|",
        ]
        for act, cnt in sorted(summary["action_counts"].items(), key=lambda p: -p[1]):
            lines.append(f"| `{act}` | {cnt} |")

        if summary["branching_screens"]:
            lines.extend([
                "",
                "## Branching Nodes (Multiple Outgoing Paths)",
                "",
                "| Screen ID | Paths | Actions |",
                "|---|---|---|",
            ])
            for b in summary["branching_screens"]:
                acts = ", ".join(f"`{a}`" for a in b["actions"])
                lines.append(f"| `{b['screen_id']}` | {b['outgoing_count']} | {acts} |")

        if summary["leaf_screens"]:
            lines.extend([
                "",
                "## Leaf Nodes (No Recorded Outgoing Transitions)",
                "",
            ])
            for l in summary["leaf_screens"]:
                lines.append(f"- `{l}`")

        return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Build and merge replay datasets")
    subparsers = parser.add_subparsers(dest="command", required=True)

    merge_p = subparsers.add_parser("merge", help="Merge several session recordings into a screen graph")
    merge_p.add_argument("sessions", nargs="+", help="Session directories to merge")
    merge_p.add_argument("--out", "-o", required=True, help="Output directory for merged dataset")
    merge_p.add_argument("--threshold", type=int, default=6, help="pHash/dHash Hamming distance threshold")
    merge_p.add_argument("--method", default="dhash", choices=["dhash", "phash"], help="Hash method")

    summary_p = subparsers.add_parser("summary", help="Export summary of a dataset or graph")
    summary_p.add_argument("target", help="Path to graph.json or dataset directory")
    summary_p.add_argument("--format", default="markdown", choices=["markdown", "json", "dot"])

    args = parser.parse_args()

    builder = DatasetBuilder(
        hash_threshold=getattr(args, "threshold", 6),
        hash_method=getattr(args, "method", "dhash"),
    )

    if args.command == "merge":
        graph = builder.merge_sessions(args.sessions, output_dir=args.out)
        print(f"Successfully merged {len(args.sessions)} sessions into {len(graph.nodes)} screens.")
        print(f"Output saved to {args.out}")
        print("\n" + builder.export_summary(graph, format="markdown"))

    elif args.command == "summary":
        target = Path(args.target)
        graph_file = target if target.is_file() else target / "graph.json"
        graph = ScreenGraph.load(graph_file)
        print(builder.export_summary(graph, format=args.format))


if __name__ == "__main__":
    main()
