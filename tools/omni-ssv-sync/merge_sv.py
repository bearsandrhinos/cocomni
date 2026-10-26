# /// script
# requires-python = ">=3.9"
# dependencies = ["ruamel.yaml>=0.18"]
# ///
"""Patch an existing Snowflake semantic view YAML with the output of ossie-snowflake.

The existing file stays authoritative for structure (table names, dimension / time_dimension /
fact classification, data types, relationship names and join types). The generated file supplies
what Omni owns: descriptions, metric expressions, and added or removed fields and metrics.

Usage: merge_sv.py --existing ssv.yaml --generated generated.yaml --out merged.yaml [--prune]
Exit code is 0 on success; a change report goes to stderr.
"""
import argparse
import re
import sys

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

FIELD_KINDS = ("dimensions", "time_dimensions", "facts")
REF = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")


def make_yaml():
    y = YAML()
    y.preserve_quotes = True
    y.width = 10000
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def norm(expr):
    return re.sub(r"\s+", " ", str(expr).replace('"', "")).strip().upper()


def upper_outside_strings(expr):
    parts = re.split(r"('(?:[^']|'')*')", str(expr))
    return "".join(p if p.startswith("'") else p.upper() for p in parts)


def table_key(table):
    bt = table.get("base_table") or {}
    return str(bt.get("table", table["name"])).upper()


def find_named(items, name):
    for item in items or []:
        if str(item["name"]).upper() == str(name).upper():
            return item
    return None


def find_field(table, name):
    for kind in FIELD_KINDS:
        item = find_named(table.get(kind), name)
        if item is not None:
            return kind, item
    return None, None


def ensure_list(table, key):
    if key not in table or table[key] is None:
        table[key] = CommentedSeq()
    return table[key]


class Merger:
    def __init__(self, existing, generated, prune):
        self.ex = existing
        self.gen = generated
        self.prune = prune
        self.report = []
        self.gen_to_ex = {}
        self.ex_tables = {table_key(t): t for t in existing["tables"]}
        for t in generated["tables"]:
            ex_t = self.ex_tables.get(table_key(t))
            if ex_t is not None:
                self.gen_to_ex[t["name"]] = ex_t

    def note(self, msg):
        self.report.append(msg)

    def resolve(self, expr, owner):
        """Rewrite `gen_table.field` references into the existing file's column expressions."""
        def sub(m):
            gen_table, field = m.group(1), m.group(2)
            ex_t = self.gen_to_ex.get(gen_table)
            if ex_t is None:
                return m.group(0)
            _, f = find_field(ex_t, field)
            col = str(f["expr"]) if f is not None else field.upper()
            return col if ex_t is owner else f"{ex_t['name']}.{col}"
        return REF.sub(sub, str(expr))

    def merge(self):
        if self.gen.get("description") and self.gen["description"] != self.ex.get("description"):
            self.note("model: description updated")
            self.ex["description"] = self.gen["description"]

        for g_t in self.gen["tables"]:
            ex_t = self.gen_to_ex.get(g_t["name"])
            if ex_t is None:
                self.note(f"table {g_t['name']}: no match in existing file (base_table {table_key(g_t)}), skipped")
                continue
            self.merge_table(g_t, ex_t)
        for key, ex_t in self.ex_tables.items():
            if ex_t not in self.gen_to_ex.values():
                self.note(f"table {ex_t['name']}: not in Omni topic, left unchanged")

        self.merge_metrics()
        self.merge_relationships()

    def merge_table(self, g_t, ex_t):
        name = ex_t["name"]
        if g_t.get("description") and g_t["description"] != ex_t.get("description"):
            ex_t["description"] = g_t["description"]
            self.note(f"{name}: description updated")

        g_pk = [c.upper() for c in (g_t.get("primary_key") or {}).get("columns", [])]
        ex_pk = [str(c).upper() for c in (ex_t.get("primary_key") or {}).get("columns", [])]
        if g_pk != ex_pk:
            self.note(f"{name}: primary_key differs (Omni {g_pk}, existing {ex_pk}), NOT changed")

        g_fields = {}
        for kind in FIELD_KINDS:
            for f in g_t.get(kind) or []:
                g_fields[str(f["name"]).upper()] = f

        for key, g_f in g_fields.items():
            kind, ex_f = find_field(ex_t, g_f["name"])
            if ex_f is None:
                new = CommentedMap(name=str(g_f["name"]).upper(), expr=upper_outside_strings(str(g_f["expr"]).replace('"', "")))
                if g_f.get("description"):
                    new["description"] = g_f["description"]
                ensure_list(ex_t, "dimensions").append(new)
                self.note(f"{name}.{new['name']}: ADDED as dimension, needs data_type and classification review")
                continue
            if g_f.get("description") and g_f["description"] != ex_f.get("description"):
                ex_f["description"] = g_f["description"]
                self.note(f"{name}.{ex_f['name']}: description updated")
            if norm(g_f["expr"]) != norm(ex_f["expr"]):
                ex_f["expr"] = upper_outside_strings(g_f["expr"])
                self.note(f"{name}.{ex_f['name']}: expr changed to {ex_f['expr']}")

        for kind in FIELD_KINDS:
            for f in list(ex_t.get(kind) or []):
                if str(f["name"]).upper() not in g_fields:
                    if self.prune:
                        ex_t[kind].remove(f)
                        self.note(f"{name}.{f['name']}: REMOVED (not in Omni topic)")
                    else:
                        self.note(f"{name}.{f['name']}: not in Omni topic, kept (use --prune to remove)")

    def metric_owner(self, expr):
        for m in REF.finditer(str(expr)):
            ex_t = self.gen_to_ex.get(m.group(1))
            if ex_t is not None:
                return ex_t
        return None

    def merge_metrics(self):
        # Generated metrics live at model level; existing ones live on tables.
        gen_metrics = list(self.gen.get("metrics") or [])
        for t in self.gen["tables"]:
            gen_metrics += list(t.get("metrics") or [])
        seen = set()
        for g_m in gen_metrics:
            seen.add(str(g_m["name"]).upper())
            owner = None
            ex_m = None
            for t in self.ex["tables"]:
                found = find_named(t.get("metrics"), g_m["name"])
                if found is not None:
                    owner, ex_m = t, found
                    break
            if ex_m is None:
                owner = self.metric_owner(g_m["expr"])
                if owner is None:
                    self.note(f"metric {g_m['name']}: cannot determine owning table, skipped")
                    continue
            expr = self.resolve(g_m["expr"], owner)
            if ex_m is None:
                new = CommentedMap(name=g_m["name"])
                if g_m.get("description"):
                    new["description"] = g_m["description"]
                new["expr"] = expr
                ensure_list(owner, "metrics").append(new)
                self.note(f"{owner['name']}.{g_m['name']}: metric ADDED ({expr})")
                continue
            if g_m.get("description") and g_m["description"] != ex_m.get("description"):
                ex_m["description"] = g_m["description"]
                self.note(f"{owner['name']}.{ex_m['name']}: description updated")
            if norm(expr) != norm(ex_m["expr"]):
                ex_m["expr"] = expr
                self.note(f"{owner['name']}.{ex_m['name']}: expr changed to {expr}")
        for t in self.ex["tables"]:
            for m in list(t.get("metrics") or []):
                if str(m["name"]).upper() in seen:
                    continue
                if t not in self.gen_to_ex.values():
                    continue
                if self.prune:
                    t["metrics"].remove(m)
                    self.note(f"{t['name']}.{m['name']}: metric REMOVED (not in Omni topic)")
                else:
                    self.note(f"{t['name']}.{m['name']}: metric not in Omni topic, kept (use --prune to remove)")

    def merge_relationships(self):
        def ex_name(gen_name):
            t = self.gen_to_ex.get(gen_name)
            return t["name"] if t is not None else gen_name

        def sig(left, right, cols):
            return (str(left).upper(), str(right).upper(),
                    tuple((str(c["left_column"]).upper(), str(c["right_column"]).upper()) for c in cols))

        existing = {sig(r["left_table"], r["right_table"], r["relationship_columns"]) for r in self.ex.get("relationships") or []}
        wanted = set()
        for g_r in self.gen.get("relationships") or []:
            left, right = ex_name(g_r["left_table"]), ex_name(g_r["right_table"])
            s = sig(left, right, g_r["relationship_columns"])
            wanted.add(s)
            if s not in existing:
                new = CommentedMap(name=f"{left}_to_{right}".lower(), left_table=left, right_table=right)
                new["join_type"] = g_r.get("join_type", "left_outer")
                new["relationship_columns"] = CommentedSeq(
                    CommentedMap(left_column=str(c["left_column"]).upper(), right_column=str(c["right_column"]).upper())
                    for c in g_r["relationship_columns"])
                ensure_list(self.ex, "relationships").append(new)
                self.note(f"relationship {new['name']}: ADDED")
        for r in list(self.ex.get("relationships") or []):
            if sig(r["left_table"], r["right_table"], r["relationship_columns"]) not in wanted:
                if self.prune:
                    self.ex["relationships"].remove(r)
                    self.note(f"relationship {r['name']}: REMOVED (not in Omni topic)")
                else:
                    self.note(f"relationship {r['name']}: not in Omni topic, kept (use --prune to remove)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--existing", required=True)
    ap.add_argument("--generated", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--prune", action="store_true", help="remove fields/metrics/relationships absent from Omni")
    args = ap.parse_args()

    y = make_yaml()
    with open(args.existing) as f:
        existing = y.load(f)
    with open(args.generated) as f:
        generated = YAML(typ="safe").load(f)

    merger = Merger(existing, generated, args.prune)
    merger.merge()
    with open(args.out, "w") as f:
        y.dump(existing, f)
    for line in merger.report:
        print(line, file=sys.stderr)
    if not merger.report:
        print("no changes", file=sys.stderr)


if __name__ == "__main__":
    main()
