"""build_catalog.py — extract a machine-readable module catalog from yaah/src.

Used by: the yaah-pipeline-authoring skill (R16 prerequisite — the "documented
module/option catalog" the skill consumes when answering "which node type?",
"which adapter?", "what args does this take, with what defaults?"). Also
consumable by humans browsing what YAAH ships.
Where: invoked from the repo root after any edit to ports/adapters/nodes/
validators — emits yaah/docs/module-catalog.md as a regeneratable artifact.
Why: avoid the documentation-vs-code drift trap. The code already carries
strict "Used by / Where / Why" docstrings ([[code-style-one-class-per-file]])
and typed constructor signatures; this script projects them into one place
the skill (and a human) can scan. Single source of truth stays the code.

HAND-WRITTEN SECTIONS (the fence contract): everything outside a fence is
overwritten on every regen. A block bounded by

    <!-- BEGIN-HANDWRITTEN: <key> -->
    ...anything...
    <!-- END-HANDWRITTEN: <key> -->

is read back out of the EXISTING docs/module-catalog.md, kept verbatim
(markers included), and re-emitted at the end of the regenerated file, in the
order the blocks appeared. Position is not preserved — only order — because the
generated sections above them are free to appear, vanish and reorder as the code
moves. Keys must be unique and fences must be balanced; either violation raises
rather than silently dropping prose a human wrote.

Targets Python 3.9+.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent / "src" / "yaah"
OUT_MD = Path(__file__).resolve().parent.parent / "docs" / "module-catalog.md"
OUT_JSON = Path(__file__).resolve().parent.parent / "docs" / "module-catalog.json"

HANDWRITTEN_BEGIN = re.compile(r"^<!--\s*BEGIN-HANDWRITTEN:\s*([\w.-]+)\s*-->\s*$")
HANDWRITTEN_END = re.compile(r"^<!--\s*END-HANDWRITTEN:\s*([\w.-]+)\s*-->\s*$")


def extract_handwritten(text: str) -> List[str]:
    """Return every fenced hand-written block of `text`, verbatim and in order.

    Each returned string starts with its BEGIN marker line and ends with its
    END marker line, so re-emitting it round-trips the fence itself.
    """
    blocks: List[str] = []
    seen: List[str] = []
    open_key: Optional[str] = None
    current: List[str] = []
    for lineno, line in enumerate(text.split("\n"), start=1):
        begin = HANDWRITTEN_BEGIN.match(line)
        end = HANDWRITTEN_END.match(line)
        if begin:
            if open_key is not None:
                raise ValueError(
                    "line {}: BEGIN-HANDWRITTEN: {} inside still-open block {}"
                    .format(lineno, begin.group(1), open_key))
            open_key = begin.group(1)
            if open_key in seen:
                raise ValueError("line {}: duplicate handwritten key {}"
                                 .format(lineno, open_key))
            seen.append(open_key)
            current = [line]
        elif end:
            if open_key is None:
                raise ValueError("line {}: END-HANDWRITTEN: {} with no BEGIN"
                                 .format(lineno, end.group(1)))
            if end.group(1) != open_key:
                raise ValueError("line {}: END-HANDWRITTEN: {} closes block {}"
                                 .format(lineno, end.group(1), open_key))
            current.append(line)
            blocks.append("\n".join(current))
            open_key, current = None, []
        elif open_key is not None:
            current.append(line)
    if open_key is not None:
        raise ValueError("unterminated handwritten block: " + open_key)
    return blocks


def _first_paragraph(text: str) -> str:
    if not text:
        return ""
    return text.split("\n\n", 1)[0].replace("\n", " ").strip()


def _parse_args_section(text: str) -> List[Dict[str, str]]:
    """Pull a Google-style 'Args:' block out of a docstring.

    Returns [{'name': str, 'desc': str}, ...] for every `    name: description`
    line in the block. Continuation lines (deeper indent) fold into the prior
    param's desc. Empty list if no Args section found.
    """
    if not text or "Args:" not in text:
        return []
    lines = text.split("\n")
    out: List[Dict[str, str]] = []
    in_args = False
    base_indent = None
    for ln in lines:
        if not in_args:
            if ln.strip() == "Args:":
                in_args = True
            continue
        stripped = ln.lstrip()
        if not stripped:
            if out:
                break  # blank line ends the block
            continue
        indent = len(ln) - len(stripped)
        if base_indent is None:
            base_indent = indent
        if indent < base_indent:
            break
        if indent == base_indent and ":" in stripped:
            name, _, desc = stripped.partition(":")
            out.append({"name": name.strip(), "desc": desc.strip()})
        elif out:
            out[-1]["desc"] = (out[-1]["desc"] + " " + stripped).strip()
    return out


def _unparse(node: Optional[ast.AST]) -> Optional[str]:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _parse_init_args(init: ast.FunctionDef) -> List[Dict[str, Any]]:
    args = init.args
    out: List[Dict[str, Any]] = []
    posargs = args.args[1:]  # skip self
    default_offset = len(posargs) - len(args.defaults)
    for i, a in enumerate(posargs):
        d = args.defaults[i - default_offset] if i >= default_offset else None
        out.append({"name": a.arg, "annotation": _unparse(a.annotation),
                    "default": _unparse(d), "kwonly": False})
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        out.append({"name": a.arg, "annotation": _unparse(a.annotation),
                    "default": _unparse(d), "kwonly": True})
    return out


def _parse_module(path: Path) -> Dict[str, Any]:
    tree = ast.parse(path.read_text())
    mod_doc = ast.get_docstring(tree) or ""
    classes = []
    functions = []
    for n in tree.body:
        if isinstance(n, ast.ClassDef):
            init_args: List[Dict[str, Any]] = []
            init_args_doc: List[Dict[str, str]] = []
            method_args_doc: List[Dict[str, Any]] = []
            for item in n.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    idoc = ast.get_docstring(item) or ""
                    ads = _parse_args_section(idoc)
                    if item.name == "__init__":
                        init_args = _parse_init_args(item)
                        init_args_doc = ads
                    elif ads and not item.name.startswith("_"):
                        method_args_doc.append({"method": item.name, "args_doc": ads})
            classes.append({"name": n.name,
                            "doc": _first_paragraph(ast.get_docstring(n) or ""),
                            "args": init_args,
                            "args_doc": init_args_doc,
                            "method_args_doc": method_args_doc})
        elif isinstance(n, ast.FunctionDef) and not n.name.startswith("_"):
            fdoc = ast.get_docstring(n) or ""
            functions.append({"name": n.name,
                              "doc": _first_paragraph(fdoc),
                              "args_doc": _parse_args_section(fdoc)})
    return {"path": str(path.relative_to(ROOT.parent.parent)),
            "summary": _first_paragraph(mod_doc),
            "classes": classes,
            "functions": functions}


def _node_type_registry() -> List[Tuple[str, str]]:
    """Read build/builders.py:default_registry() and return [(type_name, builder_fn), ...]."""
    src = (ROOT / "build" / "builders.py").read_text()
    tree = ast.parse(src)
    pairs: List[Tuple[str, str]] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "default_registry":
            for stmt in ast.walk(n):
                if (isinstance(stmt, ast.Call)
                        and isinstance(stmt.func, ast.Attribute)
                        and stmt.func.attr == "register"
                        and len(stmt.args) >= 2
                        and isinstance(stmt.args[0], ast.Constant)
                        and isinstance(stmt.args[1], ast.Name)):
                    pairs.append((stmt.args[0].value, stmt.args[1].id))
    return pairs


def _own_returns(node: ast.AST):
    """Yield the `return` statements of `node`'s own body, not those of the
    nested helper functions builders.py defines inside its builders."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(child, ast.Return):
            yield child
        for r in _own_returns(child):
            yield r


def _assigned_calls(fn: ast.AST) -> Dict[str, str]:
    """`name -> LastClass` for every `name = SomeClass(...)` in the function body.
    Needed because a builder's main construction is often assigned to a local and
    returned bare (`agent = Agent(...); ...; return agent`)."""
    out: Dict[str, str] = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    cls = _unparse(n.value.func)
                    if cls:
                        out[t.id] = cls
    return out


def _builder_constructs(builder_fn: str) -> str:
    """Name the class a builder returns.

    The PRIMARY construct is the builder's LAST own return, resolved through a local
    when that return is a bare name. Earlier, CONDITIONAL returns (an opt-in wrapper)
    are appended in parentheses rather than replacing it.

    Both rules exist for `_build_agent`, whose shape is `agent = Agent(...)` … `if
    attach_spec: return AttachingAgent(...)` … `return agent`. Taking the last
    CALL-return alone published `AttachingAgent` as what an `agent` node builds — the
    rare opt-in branch, not the thing every agent node in every pipeline actually
    is."""
    tree = ast.parse((ROOT / "build" / "builders.py").read_text())
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == builder_fn):
            continue
        locals_ = _assigned_calls(fn)
        returns = list(_own_returns(fn))
        names: List[str] = []
        for r in returns:
            if isinstance(r.value, ast.Call):
                names.append(_unparse(r.value.func) or "")
            elif isinstance(r.value, ast.Name):
                names.append(locals_.get(r.value.id, ""))
            else:
                names.append("")
        names = [n for n in names if n]
        if not names:
            return ""
        primary, rest = names[-1], [n for n in names[:-1] if n != names[-1]]
        return "{} ({})".format(primary, ", ".join(rest)) if rest else primary
    return ""


def _scan_dir(rel: str) -> List[Dict[str, Any]]:
    """Parse every non-__init__ .py file under yaah/src/yaah/<rel>/. Returns a list of module records."""
    d = ROOT / rel
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.py")):
        if p.name.startswith("__") or p.name.startswith("_"):
            continue
        out.append(_parse_module(p))
    return out


def _classes_implementing(rel: str) -> List[Dict[str, Any]]:
    """Return all non-Protocol classes under yaah/src/yaah/<rel>/ — concrete adapters.
    Falls back class doc → module-summary first sentence when the class has no docstring
    (the project's convention puts the use-case in the *module* docstring). Forwards
    args_doc + method_args_doc so the security-knob section can render them."""
    mods = _scan_dir(rel)
    out = []
    for m in mods:
        mod_doc = m["summary"]
        for c in m["classes"]:
            if c["name"].startswith("_"):
                continue
            doc = c["doc"] or mod_doc
            out.append({"module": m["path"], "name": c["name"], "doc": doc,
                        "args": c["args"],
                        "args_doc": c.get("args_doc", []),
                        "method_args_doc": c.get("method_args_doc", [])})
    return out


def _format_args(args: List[Dict[str, Any]]) -> str:
    if not args:
        return "(no constructor args)"
    parts = []
    for a in args:
        s = a["name"]
        if a["annotation"]:
            s += ": " + a["annotation"]
        if a["default"] is not None:
            s += " = " + a["default"]
        if a["kwonly"]:
            s = "*, " + s if not any("*, " in p for p in parts) else s
        parts.append(s)
    return "(" + ", ".join(parts) + ")"


def _md_section_classes(title: str, rel: str) -> str:
    items = _classes_implementing(rel)
    if not items:
        return ""
    lines = ["## " + title, ""]
    lines.append("| Class | Summary | Constructor |")
    lines.append("|---|---|---|")
    for it in items:
        args = _format_args(it["args"])
        doc = it["doc"] or "—"
        path = it["module"]
        lines.append("| `{name}` ({path}) | {doc} | `{args}` |".format(
            name=it["name"], path=path, doc=doc, args=args))
    lines.append("")
    return "\n".join(lines)


def build_catalog() -> Dict[str, Any]:
    catalog: Dict[str, Any] = {}

    # 1. Node types (the pipeline JSON `type:` values)
    catalog["node_types"] = [
        {"type": type_name, "builder": builder_fn,
         "class": _builder_constructs(builder_fn)}
        for type_name, builder_fn in _node_type_registry()]

    # 2. Ports (Protocols)
    catalog["ports"] = _extract_protocols()

    # 3. Adapters per port directory
    catalog["adapters"] = {
        "data": _classes_implementing("adapters/data"),
        "prompts": _classes_implementing("adapters/prompts"),
        "mcp": _classes_implementing("adapters/mcp"),
        "stores": _classes_implementing("adapters/stores"),
        "trace_sinks": _classes_implementing("adapters/trace"),
        "transports": _classes_implementing("adapters/transports"),
        "backends": _classes_implementing("adapters/providers"),
        "filters": _classes_implementing("adapters/filters"),
    }

    # 4. Validators
    catalog["validators"] = []
    for m in _scan_dir("."):  # yaah/src/yaah/*.py top-level
        if m["path"].endswith("validators.py"):
            catalog["validators"] = m["classes"]

    # 5. Nodes (the built-in node implementations)
    catalog["node_impls"] = _classes_implementing("nodes")

    # 6. Model-initiated tools — only the `*_tool.py` factory modules
    catalog["tools"] = []
    for m in _scan_dir("agents"):
        name = m["path"].rsplit("/", 1)[-1]
        if not name.endswith("_tool.py"):
            continue
        for f in m["functions"]:
            if f["name"].startswith("make_"):
                catalog["tools"].append({"module": m["path"], "fn": f["name"],
                                         "doc": f["doc"] or m["summary"]})

    # 7. Trace contributors
    catalog["trace_contributors"] = _classes_implementing("trace/contributors")

    return catalog


def render_markdown(catalog: Dict[str, Any]) -> str:
    md = ["# YAAH module catalog (auto-generated)",
          "",
          "Source: `yaah/src/yaah/` — regenerated by `yaah/scripts/build_catalog.py`.",
          "Drift-free by construction: the code is the truth, this file is the projection.",
          "",
          "Editing this file by hand only sticks inside a fence: a block between "
          "`<!-- BEGIN-HANDWRITTEN: key -->` and `<!-- END-HANDWRITTEN: key -->` is "
          "carried over verbatim by the generator (re-emitted at the end of the file, "
          "in fence order). Everything else is overwritten on the next regen.",
          "",
          "## Node types — pipeline JSON `type:` values",
          "",
          "These are the values valid in a pipeline's `nodes.<role>.type` field, "
          "registered in `build/builders.py:default_registry()`.",
          "",
          "**Constructs** names what the builder returns. A parenthesised class is a "
          "CONDITIONAL wrapper the builder returns instead on an opt-in key — e.g. "
          "`Agent (AttachingAgent)`: an `agent` node builds an `Agent`, and is wrapped "
          "in an `AttachingAgent` only when it declares `attach: [...]`.",
          "",
          "| `type:` | Builder | Constructs |",
          "|---|---|---|"]
    for n in catalog["node_types"]:
        md.append("| `{type}` | `{builder}` | `{cls}` |".format(
            type=n["type"], builder=n["builder"], cls=n["class"] or "—"))
    md.append("")

    # Ports section
    if catalog["ports"]:
        md.append("## Ports (Protocols)")
        md.append("")
        md.append("The structural interfaces the engine depends on. Each port has one or "
                  "more concrete adapters listed in later sections.")
        md.append("")
        md.append("| Port | Module | Methods |")
        md.append("|---|---|---|")
        for port, info in sorted(catalog["ports"].items()):
            methods = "<br>".join("`{}`".format(m) for m in info["methods"]) or "—"
            md.append("| `{p}` | `{m}` | {meths} |".format(
                p=port, m=info["module"], meths=methods))
        md.append("")

    md.append(_md_section_classes("Built-in node implementations", "nodes"))
    md.append(_md_section_classes("Validators (top-level + `validators.py`)", "."))
    md.append(_md_section_classes("Data adapters (`adapters/data/`)", "adapters/data"))
    md.append(_md_section_classes("Prompt adapters (`adapters/prompts/`)", "adapters/prompts"))
    md.append(_md_section_classes("MCP adapters (`adapters/mcp/`)", "adapters/mcp"))
    md.append(_md_section_classes("Store adapters (`adapters/stores/`)", "adapters/stores"))
    md.append(_md_section_classes("Trace-sink adapters (`adapters/trace/`)", "adapters/trace"))
    md.append(_md_section_classes("Transport adapters (`adapters/transports/`)", "adapters/transports"))
    md.append(_md_section_classes("Model-backend adapters (`adapters/providers/`)", "adapters/providers"))
    md.append(_md_section_classes("Filter adapters (`adapters/filters/`)", "adapters/filters"))
    md.append(_md_section_classes("Trace contributors (`trace/contributors/`)", "trace/contributors"))

    # Security-relevant constraints — auto-discovered from `Args:` blocks
    md.append("## Security-relevant constraints (from `Args:` docstrings)")
    md.append("")
    md.append("Auto-extracted from `Args:` blocks in the source. These are the knobs "
              "the `yaah-pipeline-authoring` skill must explain (and the user must "
              "intend) before writing — over-broad values here are the YAAH "
              "equivalent of an IaC `0.0.0.0/0`.")
    md.append("")
    # Walk every class/function across all scanned directories looking for Args blocks
    sec_entries: List[Tuple[str, str, List[Dict[str, str]]]] = []  # (label, module, args_doc)
    scan_roots = ["agents", "adapters/filters", "adapters/data", "adapters/providers",
                  "adapters/trace", "nodes", "validators.py", "build"]
    for rel in scan_roots:
        full = ROOT / rel
        if full.is_file():
            paths = [full]
        elif full.is_dir():
            paths = sorted([p for p in full.glob("*.py")
                            if not p.name.startswith("__")])
        else:
            continue
        for p in paths:
            try:
                mod = _parse_module(p)
            except SyntaxError:
                continue
            for c in mod["classes"]:
                if c["name"].startswith("_"):
                    continue
                if c["args_doc"]:
                    sec_entries.append(("`{}` (constructor)".format(c["name"]),
                                        mod["path"], c["args_doc"]))
                for mad in c["method_args_doc"]:
                    sec_entries.append(("`{}.{}`".format(c["name"], mad["method"]),
                                        mod["path"], mad["args_doc"]))
            for f in mod["functions"]:
                if f["args_doc"]:
                    sec_entries.append(("`{}()`".format(f["name"]),
                                        mod["path"], f["args_doc"]))
    if not sec_entries:
        md.append("_(no entries yet — add `Args:` blocks to the relevant docstrings.)_")
        md.append("")
    for label, mod_path, args_doc in sec_entries:
        md.append("### {} — `{}`".format(label, mod_path))
        md.append("")
        for ad in args_doc:
            md.append("- **`{}`** — {}".format(ad["name"], ad["desc"]))
        md.append("")

    # Tools
    if catalog["tools"]:
        md.append("## Model-initiated tools")
        md.append("")
        md.append("Built-in tools bound per-invocation to the Agent — referenced from "
                  "`agents/` modules.")
        md.append("")
        md.append("| Module | Item | Summary |")
        md.append("|---|---|---|")
        for t in catalog["tools"]:
            name = t.get("name") or t.get("fn", "")
            doc = t.get("doc", "—")
            md.append("| `{m}` | `{n}` | {d} |".format(m=t["module"], n=name, d=doc))
        md.append("")

    return "\n".join(md)


def regenerate(existing_text: str, catalog: Optional[Dict[str, Any]] = None) -> str:
    """Rebuild the catalog markdown, carrying `existing_text`'s hand-written
    fences over into it. Pass the previous file's text; get the next one back."""
    md = render_markdown(build_catalog() if catalog is None else catalog)
    blocks = extract_handwritten(existing_text)
    if blocks:
        md = md.rstrip("\n") + "\n\n" + "\n\n".join(blocks) + "\n"
    return md


def main() -> None:
    catalog = build_catalog()

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(catalog, indent=2, default=str))

    existing = OUT_MD.read_text() if OUT_MD.exists() else ""
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(regenerate(existing, catalog))
    print("wrote", OUT_MD)
    print("wrote", OUT_JSON)


def _extract_protocols() -> Dict[str, Dict[str, Any]]:
    """Return {port_name: {module, summary, methods}} for every runtime-checkable Protocol or Protocol class."""
    out: Dict[str, Dict[str, Any]] = {}
    for rel in ["data", "prompts", "mcp", "store", "trace", "comms", "filters", "agents"]:
        d = ROOT / rel
        if not d.exists():
            continue
        for p in sorted(d.glob("*.py")):
            if p.name.startswith("__"):
                continue
            try:
                tree = ast.parse(p.read_text())
            except SyntaxError:
                continue
            mod_doc = _first_paragraph(ast.get_docstring(tree) or "")
            for n in tree.body:
                if not isinstance(n, ast.ClassDef):
                    continue
                bases = [_unparse(b) or "" for b in n.bases]
                if not any("Protocol" in b for b in bases):
                    continue
                methods = []
                for item in n.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        sig_args = []
                        for a in item.args.args[1:]:  # skip self
                            s = a.arg
                            if a.annotation:
                                s += ": " + (_unparse(a.annotation) or "?")
                            sig_args.append(s)
                        ret = "" if item.returns is None else " -> " + (_unparse(item.returns) or "?")
                        is_async = isinstance(item, ast.AsyncFunctionDef)
                        sig = "{prefix}{name}({args}){ret}".format(
                            prefix="async " if is_async else "",
                            name=item.name, args=", ".join(sig_args), ret=ret)
                        methods.append(sig)
                out[n.name] = {"module": str(p.relative_to(ROOT.parent.parent)),
                               "summary": mod_doc,
                               "methods": methods}
    return out


if __name__ == "__main__":
    main()
