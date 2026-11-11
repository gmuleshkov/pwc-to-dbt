"""PowerCenter XML export -> small in-memory model (IR).

This module only knows the XML layout. It has no SQL or dbt knowledge.

Layout of a PowerCenter export (the parts we use):

    POWERMART / REPOSITORY / FOLDER
        SOURCE      table definitions read by mappings   (SOURCEFIELD = column)
        TARGET      table definitions written by mappings (TARGETFIELD = column)
        MAPPING
            TRANSFORMATION  logic nodes (TRANSFORMFIELD = port, TABLEATTRIBUTE = setting)
            INSTANCE        a use of a SOURCE, TARGET or TRANSFORMATION inside the mapping.
                            Connectors refer to instance names, never to definition names.
            CONNECTOR       port-level edge: FROMINSTANCE.FROMFIELD -> TOINSTANCE.TOFIELD
            MAPPINGVARIABLE $$parameters / variables
        WORKFLOW / SESSION  run order and runtime settings (only summarised, not converted)
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Port:
    """A TRANSFORMFIELD: one column flowing through a transformation."""

    name: str
    porttype: str  # e.g. "INPUT", "OUTPUT", "INPUT/OUTPUT", "LOCAL VARIABLE", "INPUT/OUTPUT/MASTER"
    datatype: str  # transformation datatype: string, decimal, double, integer, date/time, ...
    precision: int = 0
    scale: int = 0
    expression: str | None = None
    expressiontype: str | None = None  # "GENERAL" or "GROUPBY" (Aggregator)
    group: str | None = None  # Router / Union group the port belongs to
    ref_field: str | None = None  # Router: output port -> input port it copies
    is_sort_key: bool = False
    sort_direction: str = "ASCENDING"

    @property
    def parts(self) -> set[str]:
        return {p.strip() for p in self.porttype.split("/")}

    @property
    def is_input(self) -> bool:
        return "INPUT" in self.parts

    @property
    def is_output(self) -> bool:
        return "OUTPUT" in self.parts

    @property
    def is_variable(self) -> bool:
        return "LOCAL VARIABLE" in self.parts or "VARIABLE" in self.parts

    @property
    def is_master(self) -> bool:
        return "MASTER" in self.parts


@dataclass
class Group:
    name: str
    type: str  # INPUT, OUTPUT, OUTPUT/DEFAULT
    expression: str | None = None


@dataclass
class Transformation:
    name: str
    type: str  # "Expression", "Joiner", "Custom Transformation", ...
    ports: list[Port]
    attributes: dict[str, str]
    groups: list[Group] = field(default_factory=list)
    field_dependencies: list[tuple[str, str]] = field(default_factory=list)  # (input port, output port)
    description: str = ""

    def port(self, name: str) -> Port:
        for p in self.ports:
            if p.name == name:
                return p
        raise KeyError(f"{self.name} has no port {name}")

    def attr(self, name: str, default: str = "") -> str:
        return self.attributes.get(name, default)


@dataclass
class Column:
    """SOURCEFIELD / TARGETFIELD: a physical column with its database datatype."""

    name: str
    datatype: str  # native type, e.g. varchar2, number(p,s), date, timestamp
    precision: int = 0
    scale: int = 0
    nullable: bool = True
    keytype: str = "NOT A KEY"
    description: str = ""


@dataclass
class TableDef:
    name: str
    kind: str  # "source" or "target"
    columns: list[Column]
    owner: str = ""  # schema of a source (OWNERNAME)
    database_type: str = ""
    description: str = ""

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(f"{self.name} has no column {name}")


@dataclass
class Instance:
    name: str
    type: str  # SOURCE, TARGET, TRANSFORMATION
    definition: str  # name of the SOURCE/TARGET/TRANSFORMATION it instantiates
    transformation_type: str
    associated_sources: list[str] = field(default_factory=list)  # Source Qualifier -> source instances


@dataclass(frozen=True)
class Connector:
    from_instance: str
    from_field: str
    to_instance: str
    to_field: str


@dataclass
class MappingVariable:
    name: str  # includes the $$ prefix
    datatype: str
    default: str
    is_param: bool


@dataclass
class Mapping:
    name: str
    description: str
    instances: dict[str, Instance]
    transformations: dict[str, Transformation]  # by definition name
    connectors: list[Connector]
    variables: list[MappingVariable]

    def transformation_of(self, instance: str) -> Transformation:
        return self.transformations[self.instances[instance].definition]


@dataclass
class WorkflowTask:
    name: str
    type: str  # Session, Command, Start, ...
    detail: str = ""  # mapping name for sessions, command text for commands


@dataclass
class Workflow:
    name: str
    tasks: list[WorkflowTask]
    links: list[tuple[str, str]]


@dataclass
class Folder:
    name: str
    sources: dict[str, TableDef]
    targets: dict[str, TableDef]
    mappings: dict[str, Mapping]
    workflows: list[Workflow]
    unsupported_elements: list[str]  # top-level things we saw but do not convert (mapplets, shortcuts)


def _int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _transformation(el: ET.Element) -> Transformation:
    ports = [
        Port(
            name=f.get("NAME", ""),
            porttype=f.get("PORTTYPE", ""),
            datatype=f.get("DATATYPE", ""),
            precision=_int(f.get("PRECISION")),
            scale=_int(f.get("SCALE")),
            expression=f.get("EXPRESSION"),
            expressiontype=f.get("EXPRESSIONTYPE"),
            group=f.get("GROUP"),
            ref_field=f.get("REF_FIELD"),
            is_sort_key=f.get("ISSORTKEY") == "YES",
            sort_direction=f.get("SORTDIRECTION", "ASCENDING"),
        )
        for f in el.findall("TRANSFORMFIELD")
    ]
    return Transformation(
        name=el.get("NAME", ""),
        type=el.get("TYPE", ""),
        ports=ports,
        attributes={a.get("NAME", ""): a.get("VALUE", "") for a in el.findall("TABLEATTRIBUTE")},
        groups=[Group(g.get("NAME", ""), g.get("TYPE", ""), g.get("EXPRESSION")) for g in el.findall("GROUP")],
        field_dependencies=[
            (d.get("INPUTFIELD", ""), d.get("OUTPUTFIELD", "")) for d in el.findall("FIELDDEPENDENCY")
        ],
        description=el.get("DESCRIPTION", ""),
    )


def _table(el: ET.Element, kind: str) -> TableDef:
    tag = "SOURCEFIELD" if kind == "source" else "TARGETFIELD"
    return TableDef(
        name=el.get("NAME", ""),
        kind=kind,
        columns=[
            Column(
                name=f.get("NAME", ""),
                datatype=f.get("DATATYPE", ""),
                precision=_int(f.get("PRECISION")),
                scale=_int(f.get("SCALE")),
                nullable=f.get("NULLABLE", "NULL") != "NOTNULL",
                keytype=f.get("KEYTYPE", "NOT A KEY"),
                description=f.get("DESCRIPTION", ""),
            )
            for f in el.findall(tag)
        ],
        owner=el.get("OWNERNAME", ""),
        database_type=el.get("DATABASETYPE", ""),
        description=el.get("DESCRIPTION", ""),
    )


def _mapping(el: ET.Element, folder_transformations: dict[str, Transformation]) -> Mapping:
    # Reusable transformations live at folder level; mapping-local ones override them.
    transformations = dict(folder_transformations)
    transformations.update({t.name: t for t in map(_transformation, el.findall("TRANSFORMATION"))})
    instances = {}
    for i in el.findall("INSTANCE"):
        instances[i.get("NAME", "")] = Instance(
            name=i.get("NAME", ""),
            type=i.get("TYPE", ""),
            definition=i.get("TRANSFORMATION_NAME", ""),
            transformation_type=i.get("TRANSFORMATION_TYPE", ""),
            associated_sources=[a.get("NAME", "") for a in i.findall("ASSOCIATED_SOURCE_INSTANCE")],
        )
    return Mapping(
        name=el.get("NAME", ""),
        description=el.get("DESCRIPTION", ""),
        instances=instances,
        transformations=transformations,
        connectors=[
            Connector(c.get("FROMINSTANCE", ""), c.get("FROMFIELD", ""), c.get("TOINSTANCE", ""), c.get("TOFIELD", ""))
            for c in el.findall("CONNECTOR")
        ],
        variables=[
            MappingVariable(
                name=v.get("NAME", ""),
                datatype=v.get("DATATYPE", ""),
                default=v.get("DEFAULTVALUE", ""),
                is_param=v.get("ISPARAM") == "YES",
            )
            for v in el.findall("MAPPINGVARIABLE")
        ],
    )


def _workflow(el: ET.Element, folder_sessions: dict[str, str]) -> Workflow:
    """Tasks are TASKINSTANCEs; their definitions are local TASK/SESSION children or reusable
    folder-level SESSIONs (folder_sessions: session name -> mapping name)."""
    commands = {t.get("NAME", ""): " ; ".join(v.get("VALUE", "") for v in t.findall("VALUEPAIR"))
                for t in el.findall("TASK")}
    sessions = dict(folder_sessions)
    sessions.update({s.get("NAME", ""): s.get("MAPPINGNAME", "") for s in el.findall("SESSION")})
    tasks = []
    for ti in el.findall("TASKINSTANCE"):
        kind, task = ti.get("TASKTYPE", ""), ti.get("TASKNAME", "")
        detail = sessions.get(task, "") if kind == "Session" else commands.get(task, "")
        tasks.append(WorkflowTask(ti.get("NAME", ""), kind, detail))
    links = [(link.get("FROMTASK", ""), link.get("TOTASK", "")) for link in el.findall("WORKFLOWLINK")]
    return Workflow(el.get("NAME", ""), tasks, links)


SUPPORTED_FOLDER_TAGS = {"SOURCE", "TARGET", "MAPPING", "TRANSFORMATION", "WORKFLOW", "SESSION", "CONFIG",
                         "WORKLET", "TASK", "SCHEDULER"}


def load(path: str | Path) -> list[Folder]:
    """Parse an export. Returns one Folder per FOLDER element."""
    root = ET.parse(path).getroot()
    folders = []
    for f in root.iter("FOLDER"):
        folder_transformations = {t.name: t for t in map(_transformation, f.findall("TRANSFORMATION"))}
        folders.append(
            Folder(
                name=f.get("NAME", ""),
                sources={s.get("NAME", ""): _table(s, "source") for s in f.findall("SOURCE")},
                targets={t.get("NAME", ""): _table(t, "target") for t in f.findall("TARGET")},
                mappings={m.get("NAME", ""): _mapping(m, folder_transformations) for m in f.findall("MAPPING")},
                workflows=[_workflow(w, {s.get("NAME", ""): s.get("MAPPINGNAME", "") for s in f.findall("SESSION")})
                           for w in f.findall("WORKFLOW")],
                unsupported_elements=sorted(
                    {f"{c.tag} {c.get('NAME', '')}".strip() for c in f if c.tag not in SUPPORTED_FOLDER_TAGS}
                ),
            )
        )
    if not folders:
        raise ValueError(f"{path}: no FOLDER element found; is this a PowerCenter export?")
    return folders
