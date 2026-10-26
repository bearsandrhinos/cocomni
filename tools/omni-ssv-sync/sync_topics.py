# /// script
# requires-python = ">=3.9"
# dependencies = ["pyyaml"]
# ///
"""Run the Omni topic -> Snowflake semantic view chain for each changed topic file.

The target file is derived from the topic's `semantic_view:` key
(DB.SCHEMA.NAME -> <ssv-dir>/NAME.sv.yaml). Topics without that key, or whose target does not
exist yet, are skipped and reported; a first-time file needs hand classification of its columns.
Writes a markdown report for the PR body. Exits non-zero if any conversion failed.
"""
import argparse
import subprocess
import sys
from pathlib import Path

import yaml


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssv-root", required=True)
    ap.add_argument("--ssv-dir", default="cortex_project")
    ap.add_argument("--report", required=True)
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("files", nargs="*")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    sections, failed = [], False

    for name in args.files:
        topic = Path(name)
        if not topic.exists():
            sections.append(f"### `{name}`\nDeleted in Omni. The semantic view file was left unchanged.")
            continue
        sv = (yaml.safe_load(topic.read_text()) or {}).get("semantic_view")
        if not sv:
            sections.append(f"### `{name}`\nSkipped: no `semantic_view:` key.")
            continue
        target = Path(args.ssv_root) / args.ssv_dir / f"{sv.split('.')[-1]}.sv.yaml"
        if not target.exists():
            sections.append(f"### `{name}`\nSkipped: `{target.relative_to(args.ssv_root)}` does not exist. Create it once by hand so columns get classified.")
            continue

        cmd = [str(here / "run_chain.sh"), str(topic), str(target), str(target)]
        if args.prune:
            cmd.append("--prune")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            failed = True
            sections.append(f"### `{name}`\nFAILED\n```\n{proc.stderr.strip()}\n```")
            print(proc.stderr, file=sys.stderr)
            continue
        lines = [l for l in proc.stderr.splitlines() if l.strip()]
        sections.append(f"### `{name}` -> `{target.relative_to(args.ssv_root)}`\n" + "\n".join(f"- {l}" for l in lines))

    review = "\n\n**Review needed:** new fields are added as plain dimensions with no `data_type`. Fix the classification before merging.\n" \
        if any("ADDED as dimension" in s for s in sections) else ""
    Path(args.report).write_text("\n\n".join(sections) + review + "\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
