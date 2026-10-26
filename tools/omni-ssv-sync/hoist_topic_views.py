"""Hoist topic-scoped views into the views/ + relationships.yaml layout ossie-omni reads."""
import sys
from pathlib import Path
import yaml


def hoist(topic_path, out_dir):
    topic = yaml.safe_load(Path(topic_path).read_text())
    out = Path(out_dir)
    (out / "views").mkdir(parents=True, exist_ok=True)
    (out / "topics").mkdir(exist_ok=True)

    views = topic.pop("views", {})
    rels = topic.pop("relationships", [])
    catalog = topic.pop("semantic_view", "").split(".")[0]

    for name, body in views.items():
        if catalog:
            body = {"catalog": catalog, **body}
        (out / "views" / f"{name}.view.yaml").write_text(yaml.safe_dump(body, sort_keys=False))
    (out / "relationships.yaml").write_text(yaml.safe_dump(rels, sort_keys=False))

    # base view is the one that only joins outward (the fact table)
    sources = {r["join_from_view"] for r in rels}
    targets = {r["join_to_view"] for r in rels}
    base = next(iter(sources - targets))
    topic["base_view"] = base
    topic["joins"] = {v: {} for v in views if v != base}
    stem = Path(topic_path).name.split(".")[0]
    (out / "topics" / f"{stem}.topic.yaml").write_text(yaml.safe_dump(topic, sort_keys=False))


if __name__ == "__main__":
    hoist(sys.argv[1], sys.argv[2])
