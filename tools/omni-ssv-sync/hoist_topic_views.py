"""Hoist topic-scoped views into the views/ + relationships.yaml layout ossie-omni reads."""
import sys
from pathlib import Path
import yaml

AGGS = {"sum": "SUM", "avg": "AVG", "average": "AVG", "min": "MIN", "max": "MAX", "count": "COUNT"}
CMP = {"greater_than": ">", "greater_than_or_equal_to": ">=", "less_than": "<", "less_than_or_equal_to": "<="}


def literal(v):
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def condition(view, field, spec, where):
    ref = "${%s}" % (field if "." in field else f"{view}.{field}")
    if not isinstance(spec, dict) or not spec:
        raise SystemExit(f"{where}: filter on '{field}' must be a mapping of operator to value")
    parts = []
    for op, val in spec.items():
        vals = val if isinstance(val, list) else [val]
        if op in ("is", "is_not") and isinstance(val, list):
            neg = "NOT " if op == "is_not" else ""
            parts.append(f"{ref} {neg}IN ({', '.join(literal(v) for v in vals)})")
        elif op in ("is", "is_not"):
            parts.append(f"{ref} {'=' if op == 'is' else '<>'} {literal(val)}")
        elif op in CMP and not isinstance(val, list):
            parts.append(f"{ref} {CMP[op]} {literal(val)}")
        else:
            raise SystemExit(f"{where}: unsupported filter operator '{op}' on '{field}'; "
                             f"supported: is, is_not, {', '.join(CMP)}")
    return " AND ".join(parts)


def inline_measure_filters(views):
    """Ossie has no measure filters, so rewrite filtered measures as CASE WHEN raw-SQL measures."""
    for vname, view in views.items():
        for mname, m in (view.get("measures") or {}).items():
            filters = m.get("filters")
            if not filters:
                continue
            where = f"measure '{vname}.{mname}'"
            agg = m.get("aggregate_type")
            if agg not in AGGS and agg != "count_distinct":
                raise SystemExit(f"{where}: filtered measures with aggregate_type '{agg}' are not supported")
            cond = " AND ".join(condition(vname, f, spec, where) for f, spec in filters.items())
            operand = str(m["sql"]).strip() if m.get("sql") else "1"
            if agg == "count_distinct":
                call = f"COUNT(DISTINCT CASE WHEN {cond} THEN {operand} END)"
            else:
                call = f"{AGGS[agg]}(CASE WHEN {cond} THEN {operand} END)"
            m["sql"] = call
            m.pop("aggregate_type", None)
            m.pop("filters")



def hoist(topic_path, out_dir, extras_path=None):
    topic = yaml.safe_load(Path(topic_path).read_text())
    out = Path(out_dir)
    (out / "views").mkdir(parents=True, exist_ok=True)
    (out / "topics").mkdir(exist_ok=True)

    views = topic.pop("views", {})
    rels = topic.pop("relationships", [])
    inline_measure_filters(views)

    # Things the converters drop on the way to Snowflake travel in a sidecar instead.
    extras = {"fields": {}}
    if topic.get("ai_context"):
        extras["ai_context"] = str(topic.pop("ai_context")).strip()
    for vname, view in views.items():
        for fname, dim in (view.get("dimensions") or {}).items():
            if dim.get("sample_values"):
                extras["fields"].setdefault(vname, {})[fname] = {"sample_values": [str(v) for v in dim.pop("sample_values")]}
            else:
                dim.pop("sample_values", None)
    if extras_path:
        Path(extras_path).write_text(yaml.safe_dump(extras, sort_keys=False))
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
    hoist(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
