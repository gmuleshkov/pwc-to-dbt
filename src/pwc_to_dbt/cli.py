"""Command line: `pwc-to-dbt convert` and `pwc-to-dbt inspect`."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from . import model
from .convert import MappingConverter, ProjectBuilder, convert, mapping_order
from .render import write_project


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pwc-to-dbt", description="Convert Informatica PowerCenter mappings to dbt.")
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("convert", help="write a dbt project")
    c.add_argument("xml", type=Path, help="PowerCenter export (.xml)")
    c.add_argument("-o", "--out", type=Path, required=True, help="output dbt project directory")
    c.add_argument("-m", "--mapping", action="append", help="mapping to convert (repeatable; default: all)")
    c.add_argument("--folder", help="repository folder (default: the only one)")
    c.add_argument("--project-name", default="pwc_converted", help="dbt project and profile name")
    c.add_argument("--no-intermediate", action="store_true",
                   help="inline logic shared by several targets into each target model")
    c.add_argument("--strict", action="store_true", help="exit 1 if anything could not be converted")

    i = sub.add_parser("inspect", help="print what a mapping does (graph per target)")
    i.add_argument("xml", type=Path)
    i.add_argument("-m", "--mapping", action="append")
    i.add_argument("--folder")

    args = parser.parse_args(argv)
    try:
        folder = pick_folder(model.load(args.xml), args.folder)
        if args.command == "inspect":
            inspect(folder, args.mapping)
            return 0
        project = convert(folder, args.mapping, intermediate_models=not args.no_intermediate)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    write_project(project, args.out, args.project_name)
    unsupported = [x for x in project.issues if x.level == "unsupported"]
    warnings = {(x.instance, x.message) for x in project.issues if x.level == "warning"}
    print(f"wrote {len(project.models)} models to {args.out} "
          f"({len(unsupported)} unsupported, {len(warnings)} warnings; see {args.out / 'conversion_report.md'})")
    for x in unsupported:
        print(f"  UNSUPPORTED {x.mapping}/{x.instance}: {x.message}", file=sys.stderr)
    return 1 if args.strict and unsupported else 0


def pick_folder(folders: list[model.Folder], name: str | None) -> model.Folder:
    if name:
        for f in folders:
            if f.name == name:
                return f
        raise ValueError(f"no folder {name}; available: {', '.join(f.name for f in folders)}")
    if len(folders) > 1:
        raise ValueError(f"export has {len(folders)} folders; pick one with --folder")
    return folders[0]


def inspect(folder: model.Folder, mappings: list[str] | None) -> None:
    builder = ProjectBuilder(folder, mapping_order(folder))
    for name in mapping_order(folder):
        if mappings and name not in mappings:
            continue
        m = folder.mappings[name]
        conv = MappingConverter(builder, m, intermediate_models=True)
        types = Counter(conv.ttype(i) for i in m.instances)
        print(f"\n== {name}  ({', '.join(f'{t} x{c}' for t, c in types.most_common())})")
        for v in m.variables:
            print(f"   variable {v.name} ({v.datatype}) default {v.default!r}")
        order = conv.topo_order()
        for target in [n for n in order if m.instances[n].type == "TARGET"]:
            needed, stack = set(), [target]
            while stack:
                x = stack.pop()
                if x not in needed:
                    needed.add(x)
                    stack += conv.upstream(x)
            chain = [f"{x} [{conv.ttype(x)}]" for x in order if x in needed and m.instances[x].type != "SOURCE"]
            print(f"   {target}:\n      " + "\n      ".join(chain))
        shared = [b for b in conv.boundaries() if m.instances[b].type != "TARGET"]
        if shared:
            print("   shared by several targets (become intermediate models): " + ", ".join(shared))


if __name__ == "__main__":
    sys.exit(main())
