# PeakRDL-html
Generate address space documentation HTML from SystemRDL input.

## Install

Install directly from GitHub:

```bash
python3 -m pip install peakrdl git+https://github.com/Lomzem/PeakRDL-html.git
```

For local development from a checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[cli]"
```

Or use `uv` if you're cool 😎:

```bash
uv venv
source .venv/bin/activate
uv pip install peakrdl git+https://github.com/Lomzem/PeakRDL-html.git
```


## Usage

Generate one self-contained HTML file:

```bash
peakrdl html-single your_design.rdl -o design.html
```

The `-o` argument is an output file path. The parent directory must already
exist.

## Config

Some options can be configured via PeakRDL's TOML configuration file.

```toml
[html]
user_template_dir = "path/to/dir/"
user_static_dir = "path/to/dir/"
extra_doc_properties = ["list", "of", "properties"]
generate_source_links = false
reverse_fields = false
```


## Single-file Output

The single-file output embeds:

* Generated page content.
* Register model data.
* Search index data.
* Built-in CSS and JavaScript.
* Resolved local images referenced from Markdown descriptions.
* Font Awesome icon fonts used by the UI.

The single-file output does not support `user_static_dir`, since that option
implies extra files outside the generated HTML document.

## Python API

The same behavior is available from Python:

```python
from peakrdl_html import HTMLExporter

exporter = HTMLExporter()
exporter.export_single_file(root, "design.html", mathjax="cdn")
```

To disable MathJax:

```python
exporter.export_single_file(root, "design.html", mathjax="disabled")
```
