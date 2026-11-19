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


def set_list(item, key, values, label, note):
    """Set a list-valued property (synonyms, sample_values) when Omni's value differs."""
    values = [str(v) for v in values]
    if [str(v) for v in item.get(key) or []] != values:
        item[key] = CommentedSeq(values)
        note(f"{label}: {key} set to {values}")


class Merger:
    def __init__(self, existing, generated, prune, extras=None, lod_as="facts"):
        self.extras = extras or {}
        self.lod_as = lod_as
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

    def drop_model_level_lod(self, name):
        for key in ("facts", "dimensions"):
            item = find_named(self.ex.get(key), name)
            if item is not None and "lod_dimension_type" in item:
                self.ex[key].remove(item)
                self.note(f"model {key}.{item['name']}: LOD entry REMOVED")

    def guess_type(self, entry):
        if entry["agg"] in ("count", "count_distinct"):
            return "NUMBER(38,0)"
        types = []
        for view, field in entry.get("operand_fields") or []:
            ex_t = self.gen_to_ex.get(view)
            _, f = find_field(ex_t, field) if ex_t is not None else (None, None)
            if f is not None and f.get("data_type"):
                types.append(str(f["data_type"]))
        if entry["agg"] in ("min", "max", "sum") and len(set(types)) == 1:
            return types[0]
        return "FLOAT"

    def apply_lod(self):
        mode = self.lod_as
        for entry in self.extras.get("lod") or []:
            label = f"model {mode}.{entry['name'].upper()}"
            ex_t = self.gen_to_ex.get(entry["view"])
            if ex_t is None:
                continue
            # an earlier sync may have written this as a plain table dimension, which is wrong
            for kind in FIELD_KINDS:
                plain = find_named(ex_t.get(kind), entry["name"])
                if plain is not None:
                    ex_t[kind].remove(plain)
                    self.note(f"{ex_t['name']}.{plain['name']}: plain {kind[:-1]} REMOVED, it is a level-of-detail field now")
            if entry.get("skipped"):
                self.drop_model_level_lod(entry["name"])
                self.note(f"{label}: SKIPPED level of detail, {entry['skipped']}")
                continue
            for note in entry["notes"]:
                self.note(f"{label}: {note}")
            grain = []
            for view, field in entry["lod_dimensions"]:
                g_t = self.gen_to_ex.get(view)
                if g_t is None:
                    self.note(f"{label}: WARNING grain field '{view}.{field}' is not in a synced table")
                    continue
                kind, f = find_field(g_t, field)
                if f is None:
                    self.note(f"{label}: WARNING grain field '{g_t['name']}.{field}' not found, check it exists")
                elif kind == "facts":
                    self.note(f"{label}: WARNING grain field '{g_t['name']}.{f['name']}' is a fact; LOD grains need dimensions")
                grain.append((g_t["name"], f["name"] if f is not None else field.upper()))
            expr = self.resolve(re.sub(r"\$\{([^}]*)\}", r"\1", entry["expr"]), None)
            other = "dimensions" if mode == "facts" else "facts"
            current = find_named(self.ex.get(mode), entry["name"])
            if find_named(self.ex.get(other), entry["name"]) is not None:
                self.note(f"{label}: also exists under model-level {other}, left as is")
            if current is None:
                new = CommentedMap(name=entry["name"].upper())
                if entry.get("description"):
                    new["description"] = entry["description"]
                new["expr"] = expr
                new["data_type"] = self.guess_type(entry)
                new["lod_dimension_type"] = entry["lod_type"]
                new["lod_dimensions"] = CommentedSeq(CommentedMap(table=t, dimension=d) for t, d in grain)
                if entry.get("synonyms"):
                    new["synonyms"] = CommentedSeq(entry["synonyms"])
                ensure_list(self.ex, mode).append(new)
                self.note(f"{label}: ADDED {entry['lod_type']} LOD ({expr}), data_type {new['data_type']} is a guess, review it")
                continue
            if entry.get("description") and entry["description"] != current.get("description"):
                current["description"] = entry["description"]
                self.note(f"{label}: description updated")
            if norm(expr) != norm(current["expr"]):
                current["expr"] = expr
                self.note(f"{label}: expr changed to {expr}")
            if current.get("lod_dimension_type") != entry["lod_type"]:
                current["lod_dimension_type"] = entry["lod_type"]
                self.note(f"{label}: lod_dimension_type set to {entry['lod_type']}")
            old = [(str(x.get("table", "")).upper(), str(x["dimension"]).upper()) for x in current.get("lod_dimensions") or []]
            if old != [(t.upper(), d.upper()) for t, d in grain]:
                current["lod_dimensions"] = CommentedSeq(CommentedMap(table=t, dimension=d) for t, d in grain)
                self.note(f"{label}: lod_dimensions set to {[f'{t}.{d}' for t, d in grain]}")
            if entry.get("synonyms"):
                set_list(current, "synonyms", entry["synonyms"], label, self.note)

    def apply_exclusions(self):
        """Remove fields the topic's `fields:` curation excludes from the existing file."""
        for view, names in (self.extras.get("excluded") or {}).items():
            ex_t = self.gen_to_ex.get(view)
            if ex_t is None:
                continue
            for name in names:
                self.drop_model_level_lod(name)
                for kind in FIELD_KINDS + ("metrics",):
                    item = find_named(ex_t.get(kind), name)
                    if item is not None:
                        ex_t[kind].remove(item)
                        self.note(f"{ex_t['name']}.{item['name']}: REMOVED, excluded by topic fields curation")

    def apply_removals(self):
        """Remove fields that were in the previous Omni topic and are gone now. Fields that only
        exist in the Snowflake file are never touched."""
        for view, names in (self.extras.get("removed") or {}).items():
            ex_t = self.gen_to_ex.get(view)
            if ex_t is None:
                continue
            for name in names:
                self.drop_model_level_lod(name)
                found = False
                for kind in FIELD_KINDS + ("metrics",):
                    item = find_named(ex_t.get(kind), name)
                    if item is not None:
                        ex_t[kind].remove(item)
                        found = True
                        self.note(f"{ex_t['name']}.{item['name']}: REMOVED, deleted from the Omni topic")
                if not found:
                    self.note(f"{ex_t['name']}.{name}: deleted from the Omni topic, already absent here")

    def merge(self):
        self.apply_exclusions()
        self.apply_removals()
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
        self.merge_extras()
        self.apply_lod()
        self.check_synonyms()

    def merge_extras(self):
        instructions = self.extras.get("ai_context")
        if instructions:
            block = self.ex.get("module_custom_instructions")
            if block is None:
                block = CommentedMap()
                self.ex.insert(list(self.ex.keys()).index("tables"), "module_custom_instructions", block)
            if str(block.get("sql_generation") or "").strip() != instructions:
                block["sql_generation"] = instructions
                self.note("model: module_custom_instructions.sql_generation set from topic ai_context")
        for view, fields in (self.extras.get("fields") or {}).items():
            ex_t = self.gen_to_ex.get(view)
            if ex_t is None:
                continue
            for fname, props in fields.items():
                kind, ex_f = find_field(ex_t, fname)
                label = f"{ex_t['name']}.{fname}"
                if ex_f is None:
                    self.note(f"{label}: sample_values skipped, field not in existing file")
                elif kind == "facts":
                    self.note(f"{label}: sample_values skipped, Snowflake only allows them on dimensions")
                else:
                    set_list(ex_f, "sample_values", props["sample_values"], label, self.note)

    def check_synonyms(self):
        seen = {}
        for key in ("facts", "dimensions"):
            for i in self.ex.get(key) or []:
                for syn in i.get("synonyms") or []:
                    seen.setdefault(str(syn).lower(), f"model.{i['name']}")
        for t in self.ex["tables"]:
            items = [i for k in FIELD_KINDS + ("metrics",) for i in t.get(k) or []]
            for i in items:
                for syn in i.get("synonyms") or []:
                    prev = seen.setdefault(str(syn).lower(), f"{t['name']}.{i['name']}")
                    if prev != f"{t['name']}.{i['name']}":
                        self.note(f"WARNING synonym '{syn}' used on both {prev} and {t['name']}.{i['name']}; Snowflake requires unique synonyms")

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
                if g_f.get("synonyms"):
                    new["synonyms"] = CommentedSeq(g_f["synonyms"])
                ensure_list(ex_t, "dimensions").append(new)
                self.note(f"{name}.{new['name']}: ADDED as dimension, needs data_type and classification review")
                continue
            if g_f.get("description") and g_f["description"] != ex_f.get("description"):
                ex_f["description"] = g_f["description"]
                self.note(f"{name}.{ex_f['name']}: description updated")
            if norm(g_f["expr"]) != norm(ex_f["expr"]):
                ex_f["expr"] = upper_outside_strings(g_f["expr"])
                self.note(f"{name}.{ex_f['name']}: expr changed to {ex_f['expr']}")
            if g_f.get("synonyms"):
                set_list(ex_f, "synonyms", g_f["synonyms"], f"{name}.{ex_f['name']}", self.note)

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
                if g_m.get("synonyms"):
                    new["synonyms"] = CommentedSeq(g_m["synonyms"])
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
            if g_m.get("synonyms"):
                set_list(ex_m, "synonyms", g_m["synonyms"], f"{owner['name']}.{ex_m['name']}", self.note)
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
    ap.add_argument("--extras", help="sidecar written by hoist_topic_views.py (sample_values, topic ai_context)")
    ap.add_argument("--lod-as", choices=("facts", "dimensions"), default="facts",
                    help="model-level list that level-of-detail entries are written to (default: facts, as in Snowflake's examples)")
    ap.add_argument("--prune", action="store_true", help="remove fields/metrics/relationships absent from Omni")
    args = ap.parse_args()

    y = make_yaml()
    with open(args.existing) as f:
        existing = y.load(f)
    with open(args.generated) as f:
        generated = YAML(typ="safe").load(f)

    extras = None
    if args.extras:
        with open(args.extras) as f:
            extras = YAML(typ="safe").load(f)
    merger = Merger(existing, generated, args.prune, extras, args.lod_as)
    merger.merge()
    with open(args.out, "w") as f:
        y.dump(existing, f)
    for line in merger.report:
        print(line, file=sys.stderr)
    if not merger.report:
        print("no changes", file=sys.stderr)


if __name__ == "__main__":
    main()
