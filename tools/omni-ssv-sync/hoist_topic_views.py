"""Hoist topic-scoped views into the views/ + relationships.yaml layout ossie-omni reads."""
import re
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


def parse_selector(sel):
    """Return (tier, exclude, args) for one Omni topic `fields` selector.

    Tiers follow Omni's order of operations: 1 all_views.*, 2 view.*, 3 tag:x, 4 view:tag:x, 5 view.field.
    """
    sel = str(sel).strip()
    exclude = sel.startswith("-")
    body = sel[1:].strip() if exclude else sel
    if body == "all_views.*":
        return 1, exclude, ()
    m = re.fullmatch(r"(\w+)\.\*", body)
    if m:
        return 2, exclude, (m.group(1),)
    m = re.fullmatch(r"tag:(.+)", body)
    if m:
        return 3, exclude, (m.group(1),)
    m = re.fullmatch(r"(\w+):tag:(.+)", body)
    if m:
        return 4, exclude, (m.group(1), m.group(2))
    m = re.fullmatch(r"(\w+)\.(\w+)", body)
    if m:
        return 5, exclude, (m.group(1), m.group(2))
    raise SystemExit(f"topic fields: unsupported selector '{sel}'")


def apply_topic_fields(views, selectors):
    """Evaluate a topic's `fields:` list; returns {view: [excluded field names]}."""
    universe = {}  # (view, field) -> (kind, tags)
    view_tags = {}
    for vname, view in views.items():
        view_tags[vname] = set(view.get("tags") or [])
        for kind in ("dimensions", "measures"):
            for fname, f in (view.get(kind) or {}).items():
                universe[(vname, fname)] = (kind, set((f or {}).get("tags") or []))

    def matches(tier, args):
        if tier == 1:
            return set(universe)
        if tier == 2:
            return {k for k in universe if k[0] == args[0]}
        if tier == 3:
            return {k for k, (_, tags) in universe.items() if args[0] in tags or args[0] in view_tags[k[0]]}
        if tier == 4:
            return {k for k, (_, tags) in universe.items() if k[0] == args[0] and args[1] in tags}
        return {k for k in universe if k == (args[0], args[1])}

    parsed = [parse_selector(sel) for sel in selectors]
    selected = set()
    for tier in range(1, 6):
        for t, exclude, args in parsed:
            if t == tier:
                selected = selected - matches(t, args) if exclude else selected | matches(t, args)

    excluded = {}
    for (vname, fname), (kind, _) in universe.items():
        if (vname, fname) in selected:
            continue
        excluded.setdefault(vname, []).append(fname)
        field = views[vname][kind][fname] or {}
        # A primary key dimension stays in the converter input so keys and joins still resolve.
        if not field.get("primary_key"):
            del views[vname][kind][fname]
    return excluded


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



def field_names(topic):
    """{view: {field names}} for the dimensions and measures a topic defines."""
    return {v: set((body.get("dimensions") or {})) | set((body.get("measures") or {}))
            for v, body in (topic.get("views") or {}).items()}


def hoist(topic_path, out_dir, extras_path=None, previous_path=None):
    topic = yaml.safe_load(Path(topic_path).read_text())
    out = Path(out_dir)
    (out / "views").mkdir(parents=True, exist_ok=True)
    (out / "topics").mkdir(exist_ok=True)

    removed = {}
    if previous_path:
        before = field_names(yaml.safe_load(Path(previous_path).read_text()) or {})
        now = field_names(topic)
        removed = {v: sorted(names - now.get(v, set())) for v, names in before.items()
                   if v in now and names - now[v]}

    views = topic.pop("views", {})
    rels = topic.pop("relationships", [])
    inline_measure_filters(views)
    excluded = apply_topic_fields(views, topic.pop("fields")) if topic.get("fields") else {}

    # Things the converters drop on the way to Snowflake travel in a sidecar instead.
    extras = {"fields": {}, "excluded": excluded, "removed": removed}
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
    args = sys.argv[1:] + [None] * 4
    hoist(args[0], args[1], args[2], args[3])
