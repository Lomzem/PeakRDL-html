import os
import re
import time
import json
import math
import shutil
import hashlib
import base64
import mimetypes
import xml.dom.minidom
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple, Union, cast

import jinja2 as jj
import markdown
from gitmetheurl import GitMeTheURL

from systemrdl.node import FieldNode, Node, RootNode, AddressableNode, RegNode
from systemrdl.node import RegfileNode, AddrmapNode, MemNode, SignalNode
from systemrdl import rdltypes
from systemrdl.source_ref import FileSourceRef, DetailedFileSourceRef

from .stringify import stringify_rdl_value
from .search_indexer import SearchIndexer
from .__about__ import __version__

if TYPE_CHECKING:
    from systemrdl.source_ref import SourceRefBase

class _DocGroupInst:
    def __init__(self, addr_offset: int, total_size: int) -> None:
        self.addr_offset = addr_offset
        self.total_size = total_size


class DocGroupNode:
    """
    Documentation-only grouping node.

    These nodes are emitted into the HTML/RAL navigation model, but do not
    correspond to SystemRDL components.
    """
    def __init__(self, name: str) -> None:
        self.inst_name = name
        self.raw_address_offset = 0
        self.size = 0
        self.total_size = 0
        self.is_array = False
        self.array_dimensions = [] # type: List[int]
        self.array_stride = None # type: Optional[int]
        self.inst = _DocGroupInst(0, 0)
        self.children = OrderedDict() # type: OrderedDict[str, DocGroupNode]
        self.regs = [] # type: List[RegNode]
        self.abs_start = None # type: Optional[int]
        self.abs_end = None # type: Optional[int]

    def get_property(self, name: str, default: 'Any'=None) -> 'Any':
        if name == "name":
            return self.inst_name
        return default

    def get_html_name(self) -> str:
        return self.inst_name

    def get_html_desc(self, _markdown_inst: markdown.Markdown) -> None:
        return None

    def list_properties(self) -> 'List[str]':
        return []


class HTMLExporter:
    def __init__(self, **kwargs: 'Any') -> None:
        """
        Constructor for the HTML exporter class

        Parameters
        ----------
        markdown_inst: ``markdown.Markdown``
            Override the class instance of the Markdown processor.
            See the `Markdown module <https://python-markdown.github.io/reference/#Markdown>`_
            for more details.
        user_template_dir: str
            Path to a directory where user-defined template overrides are stored.
        user_static_dir: str
            Path to user-defined static content to copy to output directory.
        user_context: dict
            Additional context variables to load into the template namespace.
        show_signals: bool
            Show signal components. Default is False
        reverse_fields: bool
            (optional) Control whether register fields are displayed in reverse
            bit order (LSB to MSB). Default is False
        extra_doc_properties: List[str]
            List of properties to explicitly document.
            Nodes that have a property explicitly set will show its value in a
            table in the node's description.
            Use this to bring forward user-defined properties, or other built-in
            properties in your documentation.
        doc_group UDP
            Registers with a string ``doc_group`` property are grouped in the
            HTML hierarchy. Use ``/`` in the property value for nested groups.
        """
        self.output_dir = "" # type: str
        self.RALData = [] # type: List[Dict[str, Any]]
        self.RootNodeIds = [] # type: List[int]
        self.current_id = -1
        self.footer = "" # type: str
        self.title = "" # type: str
        self.home_url = None # type: Optional[str]
        self.skip_not_present = True
        self.reverse_fields = False
        self.current_top_node = None # type: Optional[AddressableNode]
        self.single_file_pages = None # type: Optional[Dict[int, str]]
        self._doc_group_regs = [] # type: List[RegNode]
        self._doc_group_reg_paths: Set[str] = set()

        self.user_static_dir = kwargs.pop("user_static_dir", None) # type: Optional[str]
        self.show_signals = kwargs.pop("show_signals", False)
        self.reverse_fields = kwargs.pop("reverse_fields", False)
        self.user_context = kwargs.pop("user_context", {})
        markdown_inst = kwargs.pop("markdown_inst", None) # type: Optional[markdown.Markdown]
        self.extra_properties = kwargs.pop("extra_doc_properties", []) # type: List[str]
        self.generate_source_links = kwargs.pop("generate_source_links", True)
        gmtu_translators = kwargs.pop("gitmetheurl_translators", None)
        user_template_dir = kwargs.pop("user_template_dir", None)

        # Check for stray kwargs
        if kwargs:
            raise TypeError("got an unexpected keyword argument '%s'" % list(kwargs.keys())[0])

        if markdown_inst is None:
            self.markdown_inst = markdown.Markdown(
                extensions = [
                    'extra',
                    'admonition',
                    'mdx_math',
                ],
                extension_configs={
                    'mdx_math':{
                        'add_preview': True,
                        'enable_dollar_delimiter': True,
                    }
                }
            )
        else:
            self.markdown_inst = markdown_inst

        if user_template_dir:
            loader = jj.ChoiceLoader([
                jj.FileSystemLoader(user_template_dir),
                jj.FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates"))
            ]) # type: jj.BaseLoader
        else:
            loader = jj.FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates"))

        self.jj_env = jj.Environment(
            loader=loader,
            autoescape=jj.select_autoescape(['html']),
            undefined=jj.StrictUndefined
        )

        self.gmtu = GitMeTheURL(gmtu_translators)

        self.indexer = None # type: Optional[SearchIndexer]


    def export(self, nodes: 'Union[Node, List[Node]]', output_dir: str, **kwargs: 'Dict[str, Any]') -> None:
        """
        Perform the export!

        Parameters
        ----------
        nodes: systemrdl.Node
            Top-level node to export. Can be the top-level `RootNode` or any
            internal `AddrmapNode`. Can also be a list of `RootNode` and any
            internal `AddrmapNode`.
        output_dir: str
            HTML output directory.
        footer: str
            (optional) Override footer text.
        title: str
            (optional) Override title text.
        home_url: str
            (optional) If a URL is specified, adds a home button to return to a
            parent home page.
        skip_not_present: bool
            (optional) Control whether nodes with ispresent=false are generated.
            Default is True
        """

        # if not a list
        if not isinstance(nodes, list):
            nodes = [nodes]

        # If it is the root node, skip to top addrmap
        for i, node in enumerate(nodes):
            if isinstance(node, RootNode):
                nodes[i] = node.top

        self.footer = kwargs.pop("footer", "Generated by PeakRDL-html v%s" % __version__) # type: ignore
        self.title = kwargs.pop("title", "%s Reference" % nodes[0].get_property("name")) # type: ignore
        self.home_url = kwargs.pop("home_url", None) # type: ignore
        self.skip_not_present = kwargs.pop("skip_not_present", True) # type: ignore

        # Check for stray kwargs
        if kwargs:
            raise TypeError("got an unexpected keyword argument '%s'" % list(kwargs.keys())[0])

        self.output_dir = output_dir
        self.RALData = []
        self.RootNodeIds = []
        self.current_id = -1
        self.indexer = SearchIndexer()

        # Copy static files
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        copy_recursive(static_dir, self.output_dir)
        if self.user_static_dir:
            copy_recursive(self.user_static_dir, self.output_dir)

        # Make sure output directory structure exists
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(os.path.join(self.output_dir, "content"), exist_ok=True)
        os.makedirs(os.path.join(self.output_dir, "search"), exist_ok=True)
        os.makedirs(os.path.join(self.output_dir, "data"), exist_ok=True)

        # Traverse trees
        for node in nodes:
            assert isinstance(node, AddressableNode)
            self.current_top_node = node
            self._doc_group_regs = self.collect_doc_group_regs(node)
            if node.get_property('bridge'):
                node.env.msg.warning(
                    "HTML generator does not have proper support for bridge addmaps yet. The 'bridge' property will be ignored.",
                    node.property_src_ref.get('bridge', node.inst_src_ref)
                )
            self.visit_addressable_node(node)

        # Write out RALData and other data
        self.write_ral_data()

        # Write main index.html
        self.write_index_page()

        # Write search index
        self.indexer.write_index_js(os.path.join(output_dir, "search"))


    def export_single_file(self, nodes: 'Union[Node, List[Node]]', output_file: str, **kwargs: 'Dict[str, Any]') -> None:
        """
        Export a self-contained HTML file.
        """
        mathjax = kwargs.pop("mathjax", "cdn")
        if mathjax not in ("cdn", "disabled"):
            raise ValueError("mathjax must be 'cdn' or 'disabled'")
        if self.user_static_dir:
            raise ValueError("user_static_dir is not supported by single-file HTML export")
        if output_file.endswith((os.sep, "/")) or os.path.isdir(output_file):
            raise ValueError("single-file HTML output path must be a file, not a directory")
        output_parent = os.path.dirname(os.path.abspath(output_file))
        if not os.path.isdir(output_parent):
            raise ValueError("parent directory does not exist: %s" % output_parent)

        if not isinstance(nodes, list):
            nodes = [nodes]

        for i, node in enumerate(nodes):
            if isinstance(node, RootNode):
                nodes[i] = node.top

        self.footer = kwargs.pop("footer", "Generated by PeakRDL-html v%s" % __version__) # type: ignore
        self.title = kwargs.pop("title", "%s Reference" % nodes[0].get_property("name")) # type: ignore
        self.home_url = kwargs.pop("home_url", None) # type: ignore
        self.skip_not_present = kwargs.pop("skip_not_present", True) # type: ignore

        if kwargs:
            raise TypeError("got an unexpected keyword argument '%s'" % list(kwargs.keys())[0])

        self.output_dir = output_parent
        self.RALData = []
        self.RootNodeIds = []
        self.current_id = -1
        self.indexer = SearchIndexer()
        self.single_file_pages = {}

        for node in nodes:
            assert isinstance(node, AddressableNode)
            self.current_top_node = node
            self._doc_group_regs = self.collect_doc_group_regs(node)
            if node.get_property('bridge'):
                node.env.msg.warning(
                    "HTML generator does not have proper support for bridge addmaps yet. The 'bridge' property will be ignored.",
                    node.property_src_ref.get('bridge', node.inst_src_ref)
                )
            self.visit_addressable_node(node)

        self.write_single_index_page(output_file, mathjax)
        self.single_file_pages = None


    def collect_doc_group_regs(self, top_node: AddressableNode) -> 'List[RegNode]':
        regs = [] # type: List[RegNode]
        self._doc_group_reg_paths = set()
        for node in top_node.descendants(unroll=False, skip_not_present=self.skip_not_present):
            if not isinstance(node, RegNode):
                continue
            doc_group = self.get_doc_group_path(node)
            if doc_group is not None:
                regs.append(node)
                self._doc_group_reg_paths.add(node.get_path())
        return regs


    def get_doc_group_path(self, node: RegNode) -> 'Optional[List[str]]':
        if "doc_group" not in node.list_properties():
            return None

        value = node.get_property("doc_group", default=None)
        if not isinstance(value, str) or not value:
            return None

        path = [] # type: List[str]
        for segment in value.split("/"):
            segment = segment.strip()
            if not segment:
                self.warn_invalid_doc_group(node, value, "contains an empty path segment")
                return None
            if any(c in segment for c in ".[]"):
                self.warn_invalid_doc_group(
                    node,
                    value,
                    "contains '.', '[' or ']', which conflict with HTML path syntax"
                )
                return None
            path.append(segment)
        return path


    def warn_invalid_doc_group(self, node: RegNode, value: str, reason: str) -> None:
        node.env.msg.warning(
            "Ignoring doc_group=%r on register %s because it %s." % (
                value,
                node.get_path(),
                reason
            ),
            node.property_src_ref.get("doc_group", node.inst_src_ref)
        )


    def build_doc_group_tree(self, top_node: AddressableNode) -> 'List[DocGroupNode]':
        roots = OrderedDict() # type: OrderedDict[str, DocGroupNode]

        for reg in sorted(self._doc_group_regs, key=self.get_abs_addr):
            group_path = self.get_doc_group_path(reg)
            if group_path is None:
                continue

            siblings = roots
            group = None # type: Optional[DocGroupNode]
            for segment in group_path:
                group = siblings.get(segment)
                if group is None:
                    group = DocGroupNode(segment)
                    siblings[segment] = group
                siblings = group.children

            assert group is not None
            group.regs.append(reg)

        root_groups = list(roots.values())
        top_abs = self.get_abs_addr(top_node)
        for group in root_groups:
            self.update_doc_group_range(group, top_abs)
        return root_groups


    def update_doc_group_range(self, group: DocGroupNode, parent_abs: int) -> None:
        abs_start = None # type: Optional[int]
        abs_end = None # type: Optional[int]

        for child in group.children.values():
            self.update_doc_group_range(child, 0)
            if child.abs_start is not None:
                abs_start = child.abs_start if abs_start is None else min(abs_start, child.abs_start)
                assert child.abs_end is not None
                abs_end = child.abs_end if abs_end is None else max(abs_end, child.abs_end)

        for reg in group.regs:
            reg_abs = self.get_abs_addr(reg)
            reg_end = reg_abs + reg.total_size
            abs_start = reg_abs if abs_start is None else min(abs_start, reg_abs)
            abs_end = reg_end if abs_end is None else max(abs_end, reg_end)

        if abs_start is None:
            abs_start = parent_abs
            abs_end = parent_abs

        assert abs_end is not None
        group.abs_start = abs_start
        group.abs_end = abs_end
        group.raw_address_offset = abs_start - parent_abs
        group.size = max(abs_end - abs_start, 0)
        group.total_size = group.size
        group.inst = _DocGroupInst(group.raw_address_offset, group.total_size)


    def get_abs_addr(self, node: AddressableNode) -> int:
        return int(getattr(node, "absolute_address", node.raw_address_offset))


    def structural_node_has_visible_content(self, node: AddressableNode) -> bool:
        if isinstance(node, RegNode):
            return node.get_path() not in self._doc_group_reg_paths
        if isinstance(node, MemNode):
            return True

        for child in node.children(skip_not_present=self.skip_not_present):
            if not isinstance(child, AddressableNode):
                continue
            if self.structural_node_has_visible_content(child):
                return True
        return False


    def visit_addressable_node(self, node: AddressableNode, parent_id: 'Optional[int]'=None, offset: 'Optional[int]'=None) -> int:
        self.current_id += 1
        this_id = self.current_id
        child_ids = [] # type: List[int]

        assert self.indexer is not None
        self.indexer.add_node(node, this_id)

        ral_entry: Dict[str, Any] = {
            'parent'    : parent_id,
            'children'  : child_ids,
            'name'      : node.inst_name,
            'offset'    : BigInt(node.raw_address_offset if offset is None else offset),
            'size'      : BigInt(node.size),
        }
        if node.array_dimensions:
            assert node.array_stride is not None
            ral_entry['dims'] = node.array_dimensions
            ral_entry['stride'] = BigInt(node.array_stride)
            ral_entry['idxs'] = [0] * len(node.array_dimensions)

        if isinstance(node, RegNode):
            ral_fields = []
            for i, field in enumerate(node.fields(skip_not_present=self.skip_not_present)):
                self.indexer.add_node(field, this_id, i)

                field_reset = field.get_property("reset", default=0)
                if isinstance(field_reset, Node):
                    # Reset value is a reference. Dynamic RAL data does not
                    # support this, so stuff a 0 in its place
                    field_reset = 0

                ral_field: Dict[str, Any] = {
                    'name' : field.inst_name,
                    'lsb'  : field.lsb,
                    'msb'  : field.msb,
                    'reset': BigInt(cast(int, field_reset)),
                    'disp' : 'H'
                }

                field_enum = field.get_property("encode")
                if field_enum is not None:
                    ral_field['encode'] = True
                    ral_field['disp'] = 'E'

                ral_fields.append(ral_field)

            ral_entry['fields'] = ral_fields

        # Insert entry now to ensure proper position in list
        self.RALData.append(ral_entry)

        # Insert root nodes to list
        if parent_id is None:
            self.RootNodeIds.append(this_id)

        # Recurse to children
        children = OrderedDict() # type: OrderedDict[int, Union[Node, DocGroupNode]]
        for child in node.children(skip_not_present=self.skip_not_present):
            if not isinstance(child, AddressableNode):
                continue
            if not self.structural_node_has_visible_content(child):
                continue
            child_id = self.visit_addressable_node(child, this_id)
            child_ids.append(child_id)
            children[child_id] = child

        if parent_id is None and self._doc_group_regs:
            for doc_group in self.build_doc_group_tree(node):
                child_id = self.visit_doc_group_node(doc_group, this_id, self.get_abs_addr(node))
                child_ids.append(child_id)
                children[child_id] = doc_group

            self.sort_children_by_offset(child_ids, children)

        # Generate page for this node
        self.write_page(this_id, node, children)

        return this_id


    def visit_doc_group_node(self, node: DocGroupNode, parent_id: int, parent_abs: int) -> int:
        self.current_id += 1
        this_id = self.current_id
        child_ids = [] # type: List[int]

        offset = node.abs_start - parent_abs if node.abs_start is not None else node.raw_address_offset
        node.raw_address_offset = offset
        node.inst = _DocGroupInst(offset, node.total_size)

        ral_entry: Dict[str, Any] = {
            'parent'    : parent_id,
            'children'  : child_ids,
            'name'      : node.inst_name,
            'offset'    : BigInt(offset),
            'size'      : BigInt(node.size),
            'doc_group' : True,
        }
        self.RALData.append(ral_entry)

        children = OrderedDict() # type: OrderedDict[int, Union[Node, DocGroupNode]]
        group_abs = node.abs_start if node.abs_start is not None else parent_abs

        for child_group in node.children.values():
            child_id = self.visit_doc_group_node(child_group, this_id, group_abs)
            child_ids.append(child_id)
            children[child_id] = child_group

        for reg in node.regs:
            reg_offset = self.get_abs_addr(reg) - group_abs
            child_id = self.visit_addressable_node(reg, this_id, reg_offset)
            child_ids.append(child_id)
            children[child_id] = reg

        self.sort_children_by_offset(child_ids, children)
        self.write_page(this_id, node, children)
        return this_id


    def sort_children_by_offset(self, child_ids: 'List[int]', children: 'OrderedDict[int, Union[Node, DocGroupNode]]') -> None:
        def sort_key(item: 'Tuple[int, Union[Node, DocGroupNode]]') -> 'Tuple[int, str]':
            child = cast(Any, item[1])
            return child.raw_address_offset, child.inst_name

        ordered_items = sorted(
            children.items(),
            key=sort_key
        )
        child_ids[:] = [child_id for child_id, _child in ordered_items]
        children.clear()
        children.update(ordered_items)


    def write_ral_data(self) -> None:
        N_RAL_NODES_PER_FILE = 16384
        n_files = math.ceil(len(self.RALData)/N_RAL_NODES_PER_FILE)

        # Write data index
        PageInfo = {
            "title" : self.title
        }
        path = os.path.join(self.output_dir, "data/data_index.js")
        with open(path, 'w', encoding='utf-8') as f:
            f.write("var N_RAL_FILES = %d;\n" % n_files)
            f.write("var N_RAL_NODES_PER_FILE = %d;\n" % N_RAL_NODES_PER_FILE)

            f.write("var RootNodeIds = ")
            f.write(PeakRDLJSEncoder(separators=(',', ':')).encode(self.RootNodeIds))
            f.write(";\n")

            f.write("var PageInfo = ")
            f.write(PeakRDLJSEncoder(separators=(',', ':')).encode(PageInfo))
            f.write(";\n")

        # Write RALData files
        for file_idx in range(n_files):
            start = file_idx * N_RAL_NODES_PER_FILE
            end = min((file_idx + 1) * N_RAL_NODES_PER_FILE, len(self.RALData))
            path = os.path.join(self.output_dir, "data/ral-data-%d.json" % file_idx)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(PeakRDLJSEncoder(separators=(',', ':')).encode(self.RALData[start:end]))


    def get_embedded_ral_data(self) -> 'Dict[str, Any]':
        N_RAL_NODES_PER_FILE = 16384
        n_files = math.ceil(len(self.RALData)/N_RAL_NODES_PER_FILE)
        ral_files = []
        for file_idx in range(n_files):
            start = file_idx * N_RAL_NODES_PER_FILE
            end = min((file_idx + 1) * N_RAL_NODES_PER_FILE, len(self.RALData))
            ral_files.append(self.RALData[start:end])
        return {
            "N_RAL_FILES": n_files,
            "N_RAL_NODES_PER_FILE": N_RAL_NODES_PER_FILE,
            "RootNodeIds": self.RootNodeIds,
            "PageInfo": {"title": self.title},
            "EmbeddedRALData": ral_files,
        }


    _template_map = {
        AddrmapNode : "addrmap.html",
        RegfileNode : "regfile.html",
        MemNode     : "mem.html",
        RegNode     : "reg.html",
    }

    def write_page(self, this_id: int, node: 'Union[Node, DocGroupNode]', children: 'Dict[int, Union[Node, DocGroupNode]]') -> None:
        text = self.render_page(this_id, node, children)
        if self.single_file_pages is not None:
            self.single_file_pages[this_id] = text
            return

        uid = self.get_node_uid(this_id)
        output_path = os.path.join(self.output_dir, "content", "%s.html" % uid)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(text)


    def render_page(self, this_id: int, node: 'Union[Node, DocGroupNode]', children: 'Dict[int, Union[Node, DocGroupNode]]') -> str:

        def field_order(x: 'Any') -> 'Any':
            if not self.reverse_fields:
                return reversed(x)
            else:
                return x

        view_source_url, view_source_filename= self.get_view_source_info(node)
        context = {
            'this_id': this_id,
            'node' : node,
            'children' : children,
            'has_description' : has_description,
            'friendly_access' : friendly_access,
            'has_enum_encoding' : has_enum_encoding,
            'get_enum_desc': self.get_enum_html_desc,
            'get_node_desc': self.get_node_html_desc,
            'get_child_addr_digits': self.get_child_addr_digits,
            'get_node_path': self.get_node_path,
            'get_node_offset': self.get_node_offset,
            'get_node_abs_addr': self.get_node_abs_addr,
            'get_node_total_size': self.get_node_total_size,
            'get_table_addr_digits': self.get_table_addr_digits,
            'format_addr': self.format_addr,
            'show_signals': self.show_signals,
            'has_extra_property_doc': self.has_extra_property_doc,
            'extra_properties': self.extra_properties,
            'stringify_rdl_value': stringify_rdl_value,
            'SignalNode' : SignalNode,
            'FieldNode': FieldNode,
            'AddressableNode': AddressableNode,
            'PropertyReference': rdltypes.PropertyReference,
            'reversed': field_order,
            'isinstance': isinstance,
            'list': list,
            'view_source_url': view_source_url,
            'view_source_filename': view_source_filename,
            'reg_fields_are_low_to_high': reg_fields_are_low_to_high,
            'skip_not_present': self.skip_not_present,
            'highest_fields_first': not self.reverse_fields
        }
        context.update(self.user_context)

        template_name = "doc_group.html"
        if not isinstance(node, DocGroupNode):
            template_name = self._template_map[type(node)] # type: ignore[index]
        template = self.jj_env.get_template(template_name)
        return template.render(context)


    def write_index_page(self) -> None:
        context = {
            'title': self.title,
            'footer_text': self.footer,
            'home_url': self.home_url,
            # propagate build timestamp to some URLs to force cache invalidation when rebuilt
            'build_ts': int(time.time()),
            'version': __version__,
        }
        context.update(self.user_context)

        template = self.jj_env.get_template("index.html")
        stream = template.stream(context)
        output_path = os.path.join(self.output_dir, "index.html")
        stream.dump(output_path)


    def write_single_index_page(self, output_file: str, mathjax: str) -> None:
        assert self.indexer is not None
        assert self.single_file_pages is not None

        search_bucket_index, search_buckets = self.indexer.get_index_data()
        ral_data = self.get_embedded_ral_data()
        embedded_data_js = "\n".join([
            "var N_RAL_FILES = %s;" % ral_data["N_RAL_FILES"],
            "var N_RAL_NODES_PER_FILE = %s;" % ral_data["N_RAL_NODES_PER_FILE"],
            "var RootNodeIds = %s;" % self.js_json(ral_data["RootNodeIds"]),
            "var PageInfo = %s;" % self.js_json(ral_data["PageInfo"]),
            "var EmbeddedRALData = %s;" % self.js_json(ral_data["EmbeddedRALData"]),
            "var EmbeddedContent = %s;" % self.js_json(self.single_file_pages),
            "var SearchBucketIndex = %s;" % self.js_json(search_bucket_index),
            "var EmbeddedSearchBuckets = %s;" % self.js_json(search_buckets),
        ])

        context = {
            'title': self.title,
            'footer_text': self.footer,
            'home_url': self.home_url,
            'build_ts': int(time.time()),
            'version': __version__,
            'inline_css': self.get_single_file_css(),
            'inline_js': self.get_single_file_js(),
            'embedded_data_js': embedded_data_js,
            'mathjax': mathjax,
        }
        context.update(self.user_context)

        template = self.jj_env.get_template("index_single.html")
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(template.render(context))


    def js_json(self, obj: 'Any') -> str:
        s = PeakRDLJSEncoder(separators=(',', ':')).encode(obj)
        # Prevent embedded HTML like </script> from terminating the data script.
        return s.replace("</", "<\\/")


    def get_single_file_css(self) -> str:
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        css_parts = []
        for relpath in ("css/normalize.css", "css/layout.css", "css/theme.css"):
            path = os.path.join(static_dir, relpath)
            with open(path, 'r', encoding='utf-8') as f:
                css = f.read()
            if relpath == "css/theme.css":
                css = self.prepare_single_file_theme_css(css, static_dir)
            css_parts.append(css)
        return "\n".join(css_parts)


    def prepare_single_file_theme_css(self, css: str, static_dir: str) -> str:
        css = re.sub(
            r'@font-face\s*\{\s*font-family:\s*"(?:Lato|Roboto Slab)";.*?\}',
            '',
            css,
            flags=re.DOTALL
        )

        def font_data_uri(relpath: str) -> str:
            path = os.path.join(static_dir, relpath)
            with open(path, 'rb') as f:
                encoded = base64.b64encode(f.read()).decode('ascii')
            return "data:font/woff2;base64,%s" % encoded

        fa_solid = font_data_uri("fonts/FontAwesome/fa-solid-900.woff2")
        fa_regular = font_data_uri("fonts/FontAwesome/fa-regular-400.woff2")

        css = re.sub(
            r"@font-face\s*\{\s*font-family:\s*'Font Awesome 5 Free';\s*font-weight:\s*900;.*?\}",
            "@font-face {\n"
            "    font-family: 'Font Awesome 5 Free';\n"
            "    font-weight: 900;\n"
            "    font-style: normal;\n"
            "    src: url(\"%s\") format(\"woff2\");\n"
            "}" % fa_solid,
            css,
            flags=re.DOTALL
        )
        css = re.sub(
            r"@font-face\s*\{\s*font-family:\s*'Font Awesome 5 Free';\s*font-weight:\s*400;.*?\}",
            "@font-face {\n"
            "    font-family: 'Font Awesome 5 Free';\n"
            "    font-weight: 400;\n"
            "    font-style: normal;\n"
            "    src: url(\"%s\") format(\"woff2\");\n"
            "}" % fa_regular,
            css,
            flags=re.DOTALL
        )
        return css


    def get_single_file_js(self) -> str:
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        js_parts = []
        for relpath in (
            "js/progressbar.min.js",
            "js/sha1.js",
            "js/ral.js",
            "js/main.js",
            "js/nav.js",
            "js/sidebar.js",
            "js/index_edit.js",
            "js/field_testers.js",
            "js/search.js",
            "js/address_search.js",
            "js/path_search.js",
            "js/content_search.js",
        ):
            path = os.path.join(static_dir, relpath)
            with open(path, 'r', encoding='utf-8') as f:
                js_parts.append(f.read())
        js = "\n".join(js_parts)
        js = js.replace(
            '        var path = "data/ral-data-" + idx + ".json?ts=" + BUILD_TS;\n'
            '        var awaitable = fetch(path)',
            '        var awaitable = Promise.reject(new Error("embedded RAL data missing"))'
        )
        js = js.replace(
            '    var path = "content/" + RAL.get_node_uid(id) + ".html?ts=" + BUILD_TS;\n'
            '    var awaitable = fetch(path)',
            '    var awaitable = Promise.reject(new Error("embedded page content missing"))'
        )
        js = js.replace(
            '        var path = "search/bkt-" + bidx + ".json?ts=" + BUILD_TS;\n\n'
            '        var awaitable = fetch(path)',
            '        var awaitable = Promise.reject(new Error("embedded search bucket missing"))'
        )
        return js.replace("</", "<\\/")


    def get_child_addr_digits(self, node: AddressableNode) -> int:
        return math.ceil(math.log2(node.size) / 4)


    def get_node_offset(self, node_id: int) -> int:
        offset = self.RALData[node_id]["offset"]
        assert isinstance(offset, BigInt)
        return offset.v


    def get_node_total_size(self, node_id: int) -> int:
        node = self.RALData[node_id]
        size = node["size"]
        assert isinstance(size, BigInt)

        if "dims" not in node:
            return size.v

        stride = node["stride"]
        dims = node["dims"]
        assert isinstance(stride, BigInt)
        assert isinstance(dims, list)

        num_elements = 1
        for dim in dims:
            num_elements *= dim
        return stride.v * (num_elements - 1) + size.v


    def get_node_abs_addr(self, node_id: int) -> int:
        addr = self.get_node_offset(node_id)
        parent_id = self.RALData[node_id]["parent"]
        while parent_id is not None:
            assert isinstance(parent_id, int)
            addr += self.get_node_offset(parent_id)
            parent_id = self.RALData[parent_id]["parent"]
        return addr


    def get_table_addr_digits(self, this_id: int, child_ids: 'List[int]') -> int:
        max_addr = self.get_node_abs_addr(this_id)
        for child_id in child_ids:
            child_end = self.get_node_abs_addr(child_id) + self.get_node_total_size(child_id) - 1
            max_addr = max(max_addr, child_end)
        return max(2, math.ceil(max_addr.bit_length() / 4))


    def format_addr(self, addr: int, digits: int) -> str:
        return "0x{n:0{width}X}".format(n=addr, width=digits)


    def get_node_html_desc(self, node: Node, increment_heading: int=0) -> 'Optional[str]':
        """
        Wrapper function to get HTML description
        If no description, returns None

        Performs the following transformations on top of the built-in HTML desc
        output:
        - Increment any heading tags
        - Transform img paths that point to local files. Copy referenced image to output
        """

        desc = node.get_html_desc(self.markdown_inst)
        if desc is None:
            return desc

        # Keep HTML semantically correct by promoting heading tags if desc ends
        # up as a child of existing headings.
        if increment_heading > 0:
            def heading_replace_callback(m: 're.Match') -> str:
                new_heading = "<%sh%d>" % (
                    m.group(1),
                    min(int(m.group(2)) + increment_heading, 6)
                )
                return new_heading
            desc = re.sub(r'<(/?)[hH](\d)>', heading_replace_callback, desc)

        # Transform image references
        # If an img reference points to a file on the local filesystem, then
        # copy it to the output and transform the reference
        if increment_heading > 0:
            def img_transform_callback(m: 're.Match') -> str:
                dom = xml.dom.minidom.parseString(m.group(0))
                img_node = dom.childNodes[0]
                assert img_node.attributes is not None
                img_src = img_node.attributes["src"].value

                if os.path.isabs(img_src):
                    # Absolute local path, or root URL
                    pass
                elif re.match(r'(https?|file)://', img_src):
                    # Absolute URL
                    pass
                else:
                    # Looks like a relative path
                    # See if it points to something relative to the source file
                    path = self.try_resolve_rel_path(node.def_src_ref, img_src)
                    if path is not None:
                        img_src = path

                if os.path.exists(img_src):
                    if self.single_file_pages is not None:
                        with open(img_src, 'rb') as f:
                            data = f.read()
                        mime_type = mimetypes.guess_type(img_src)[0] or "application/octet-stream"
                        encoded = base64.b64encode(data).decode('ascii')
                        img_node.attributes["src"].value = "data:%s;base64,%s" % (mime_type, encoded)
                    else:
                        with open(img_src, 'rb') as f:
                            md5 = hashlib.md5(f.read()).hexdigest()
                        new_path = os.path.join(
                            self.output_dir, "content",
                            "%s_%s" % (md5[0:8], os.path.basename(img_src))
                        )
                        shutil.copyfile(img_src, new_path)
                        img_node.attributes["src"].value = os.path.join(
                            "content",
                            "%s_%s" % (md5[0:8], os.path.basename(img_src))
                        )
                    return dom.childNodes[0].toxml()

                return m.group(0)

            desc = re.sub(r'<\s*img.*/>', img_transform_callback, desc)
        return desc


    def get_enum_html_desc(self, enum_member) -> str: # type: ignore
        s = enum_member.get_html_desc(self.markdown_inst)
        if s:
            return s
        else:
            return ""


    def try_resolve_rel_path(self, src_ref: 'Optional[SourceRefBase]', relpath: str) -> 'Optional[str]':
        """
        Test if the source reference's base path + the relpath points to a file
        If it works, returns the new path.
        If not, return None
        """

        if not isinstance(src_ref, FileSourceRef):
            return None

        path = os.path.join(os.path.dirname(src_ref.path), relpath)
        if not os.path.exists(path):
            return None

        return path


    def has_extra_property_doc(self, node: Node) -> bool:
        """
        Returns True if node has a property set that is to be explicitly
        documented.
        """
        for prop in self.extra_properties:
            if prop in node.list_properties():
                return True
        return False

    def get_view_source_info(self, node: 'Union[Node, DocGroupNode]') -> 'Tuple[Optional[str], Optional[str]]':
        """
        Attempt to derive the node definition's source code sharelink using
        GitMeTheURL.

        Returns None if not found
        """
        if not self.generate_source_links:
            return None, None
        if isinstance(node, DocGroupNode):
            return None, None

        src_ref = node.def_src_ref or node.inst_src_ref
        if isinstance(src_ref, DetailedFileSourceRef):
            path = src_ref.path
            line = src_ref.line
        elif isinstance(src_ref, FileSourceRef):
            path = src_ref.path
            line = None
        else:
            return None, None

        # resolve any symlinks to ensure true git path
        path = os.path.realpath(path)

        try:
            return (self.gmtu.get_source_url(path, line), os.path.basename(path))
        except Exception: # pylint: disable=broad-except
            return None, None

    def get_node_uid(self, node_id: int) -> str:
        """
        Returns the node's UID string
        """
        node_path = self.get_node_path(node_id)
        path_hash = hashlib.sha1(node_path.encode('utf-8')).hexdigest()
        return path_hash


    def get_node_path(self, node_id: int) -> str:
        path_segments = [] # type: List[str]
        current_id = node_id # type: Optional[int]
        while current_id is not None:
            node = self.RALData[current_id]
            path_segments.insert(0, node["name"])
            current_id = node["parent"]
        return ".".join(path_segments)


def has_description(node: 'Union[Node, DocGroupNode]') -> bool:
    """
    Test if node has a description defined
    """
    return "desc" in node.list_properties()

def friendly_access(obj: 'Any') -> str:
    """
    Convert access types into a human-friendly string
    """
    lut = {
        rdltypes.AccessType.na      : "Not Accessible",
        rdltypes.AccessType.rw      : "Readable and Writable",
        rdltypes.AccessType.r       : "Read-only",
        rdltypes.AccessType.w       : "Write-only",
        rdltypes.AccessType.rw1     : "Readable. Writable once.",
        rdltypes.AccessType.w1      : "Writable once",
        rdltypes.OnReadType.rclr    : "Clear on read",
        rdltypes.OnReadType.rset    : "Set on read",
        rdltypes.OnWriteType.woset  : "Bitwise write 1 to set",
        rdltypes.OnWriteType.woclr  : "Bitwise write 1 to clear",
        rdltypes.OnWriteType.wot    : "Bitwise write 1 to toggle",
        rdltypes.OnWriteType.wzs    : "Bitwise write 0 to set",
        rdltypes.OnWriteType.wzc    : "Bitwise write 0 to clear",
        rdltypes.OnWriteType.wzt    : "Bitwise write 0 to toggle",
        rdltypes.OnWriteType.wclr   : "Clear on write",
        rdltypes.OnWriteType.wset   : "Set on write",
    }
    return lut.get(obj, "")


def has_enum_encoding(field: FieldNode) -> bool:
    """
    Test if field is encoded with an enum
    """
    return "encode" in field.list_properties()


def reg_fields_are_low_to_high(node: RegNode) -> bool:
    for field in node.fields():
        if field.msb < field.lsb:
            return True
    return False

def copy_recursive(src: str, dst: str) -> None:
    """
    distutils.dir_util.copy_tree is deprecated, and shutil.copytree does not have
    the dirs_exist_ok option until py3.8.
    Implement an equivalent
    """
    os.makedirs(dst, exist_ok=True)

    for entry in os.listdir(src):
        spath = os.path.join(src, entry)
        dpath = os.path.join(dst, entry)
        if os.path.isdir(spath):
            copy_recursive(spath, dpath)
        else:
            shutil.copyfile(spath, dpath)


class BigInt:
    def __init__(self, v: int):
        self.v = v

class PeakRDLJSEncoder(json.JSONEncoder):
    def default(self, o: 'Any') -> str: # pylint: disable=method-hidden
        if isinstance(o, BigInt):
            # store bigInt integers as hex string. JS will convert to bigInt objects post-load.
            return "%x" % o.v
        else:
            return super().default(o)
