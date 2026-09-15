# Knowledge Graph Schema

## Node Types

### File
Represents a source code file.

| Property | Type | Description |
|----------|------|-------------|
| name | string | Absolute file path |
| file_path | string | Same as name for File nodes |
| language | string | Detected language (python, typescript, go, etc.) |
| line_start | int | Always 1 |
| line_end | int | Total line count |
| file_hash | string | SHA-256 of file contents (for change detection) |

### Class
Represents a class, struct, interface, enum, or module definition.

| Property | Type | Description |
|----------|------|-------------|
| name | string | Class name |
| file_path | string | File containing the class |
| line_start | int | Definition start line |
| line_end | int | Definition end line |
| language | string | Source language |
| parent_name | string? | Enclosing class (for nested classes) |
| modifiers | string? | Access modifiers (public, abstract, etc.) |

### Function
Represents a function, method, or constructor definition.

| Property | Type | Description |
|----------|------|-------------|
| name | string | Function name |
| file_path | string | File containing the function |
| line_start | int | Definition start line |
| line_end | int | Definition end line |
| language | string | Source language |
| parent_name | string? | Enclosing class (for methods) |
| params | string? | Parameter list as source text |
| return_type | string? | Return type annotation |
| is_test | bool | Whether this is a test function |

### Test
Same schema as Function, but `kind = "Test"` and `is_test = true`. Identified by:
- Name starts with `test_` or `Test`
- Name ends with `_test` or `_spec`
- File matches test file patterns (`test_*.py`, `*.test.ts`, `*_test.go`, etc.)
- Language-specific test markers where supported, such as common Rust test attributes

### Type
Represents a type alias, interface, enum, struct-like type, or parser-specific type construct where the language exposes one.

| Property | Type | Description |
|----------|------|-------------|
| name | string | Type name |
| file_path | string | File containing the type |
| line_start | int | Definition start line |
| line_end | int | Definition end line |

### Endpoint
A synthesised node representing a routed entry point, emitted by the Spring enrichment for request mappings. Linked to the method that services it by a `HANDLES` edge.

### Scheduler
A synthesised node representing a scheduled invocation, emitted for `@Scheduled` methods. Linked to the method it fires by a `TRIGGERS` edge.

### ConfigProperty
An externalised configuration key parsed out of Spring `application.properties` / `application.yml` files. Values are deliberately discarded — only the key is stored. Linked to the code that binds it by a `DEPENDS_ON_CONFIG` edge.

## Edge Types

### CALLS
A function calls another function.

| Property | Type | Description |
|----------|------|-------------|
| source | string | Qualified name of the caller |
| target | string | Name of the called function (may be unqualified) |
| file_path | string | File where the call occurs |
| line | int | Line number of the call |

### IMPORTS_FROM
A file imports from another module or file.

| Property | Type | Description |
|----------|------|-------------|
| source | string | Importing file path |
| target | string | Imported module/path |
| file_path | string | Same as source |
| line | int | Line number of the import |

### INHERITS
A class extends/inherits from another class.

| Property | Type | Description |
|----------|------|-------------|
| source | string | Child class qualified name |
| target | string | Parent class name |
| file_path | string | File containing the child class |

### IMPLEMENTS
A class implements an interface (Java, C#, TypeScript, Go).

| Property | Type | Description |
|----------|------|-------------|
| source | string | Implementing class |
| target | string | Interface name |

### CONTAINS
Structural containment: a file contains a class, a class contains a method.

| Property | Type | Description |
|----------|------|-------------|
| source | string | Container (file path or class qualified name) |
| target | string | Contained node qualified name |

### TESTED_BY
A function is tested by a test function.

| Property | Type | Description |
|----------|------|-------------|
| source | string | Function being tested |
| target | string | Test function qualified name |

### DEPENDS_ON
General dependency relationship (used for non-specific dependencies).

### REFERENCES
A value-level reference to another symbol, often used for function-as-value patterns such as callback maps, arrays, or assignment.

The JSP/HTML resolver also emits `REFERENCES` edges from page File nodes (`.jsp`/`.jspf`/`.tag`/`.html`) to the frontend assets they load, with `extra.asset` distinguishing the flavour:

| Property | Type | Description |
|----------|------|-------------|
| source | string | Page File node (absolute path) |
| target | string | Asset File node (absolute path: js/css/jsp/html) |
| file_path | string | Same as source |
| line | int | Line of the `script src` / `link href` / `a href` / `form action` |
| extra.asset | string | `script`, `stylesheet`, or `page` |
| extra.href | string | The raw href as written in the page |

Targets resolve against the graph's own File nodes: relative to the referencing page's directory first, then under the configured `web_root`, then with any configured servlet context path (`context_paths`) stripped. Unresolvable hrefs (external URLs, EL expressions, missing files) produce no edge and are counted in the resolver's `unresolved_references` stat.

### RENDERS
A JSP page renders through an explicitly named Java bean (`beanclass`-style attribute). Emitted by the JSP resolver from `jsp` File nodes.

| Property | Type | Description |
|----------|------|-------------|
| source | string | Page File node (absolute path) |
| target | string | Java Class node qualified name, or the raw dotted FQN when unresolvable |
| file_path | string | Same as source |
| line | int | Line of the bean-binding attribute |
| extra.fqn | string | The raw dotted FQN read off the attribute |
| extra.resolution | string | `class` when bound to a Class node, `raw` otherwise |
| extra.unresolved | bool | Present and `true` when the target is a raw FQN matching no node |

### REQUESTS
A JSP page or plain `.js` file calls a route-annotated Java endpoint: `href`/`action`/`url` attributes and `url:`/`fetch('...')` literals matched against known routes. Emitted by the JSP resolver from `jsp` and `javascript` File nodes.

| Property | Type | Description |
|----------|------|-------------|
| source | string | Page or script File node (absolute path) |
| target | string | Endpoint node, Java Class node, or raw dotted FQN — see binding order below |
| file_path | string | Same as source |
| line | int | Line of the URL literal |
| extra.route | string | Normalized route key the URL reduced to |
| extra.url | string | The raw URL as written |
| extra.fqn | string | Raw dotted FQN, when a class-level binding matched |
| extra.resolution | string | `endpoint`, `class`, or `raw` |
| extra.unresolved | bool | Present and `true` when the target is a raw FQN matching no node |

Target binding order (highest first):

1. **Endpoint node** — the route matches a parser-emitted Spring `Endpoint` (`extra.route`/`extra.http_method`); handler-method visibility comes from the Endpoint's existing `HANDLES` edge. When several endpoints share a route (e.g. GET and POST on one path), the GET endpoint wins, then the smallest qualified name — deterministic without pretending to know the verb.
2. **Java Class node** — the route maps (via `route_annotations` on classes, e.g. Stripes `@UrlBinding`) to a dotted FQN, which binds to a Class node by unique repository path-suffix match.
3. **Raw FQN** — no node matches; the edge keeps the dotted FQN target with `extra.unresolved = true` so the link stays visible instead of silently dropping.

### INCLUDES
A JSP page statically includes another (`<%@ include file="..." %>` / `<jsp:include page="...">`). Target is the included page's File node; the same href resolution rules as `REFERENCES` apply.

| Property | Type | Description |
|----------|------|-------------|
| source | string | Including page File node (absolute path) |
| target | string | Included page File node (absolute path) |
| file_path | string | Same as source |
| line | int | Line of the include directive/tag |
| extra.href | string | The raw include path as written |

### INJECTS
A dependency-injection relationship, currently used by Java/Spring enrichment for injected fields and constructor parameters.

### CONSUMES / PRODUCES
Data or event flow relationships emitted by specialised parsers when a source consumes or produces a named resource.

### TEMPORAL_STUB
Temporal dependency placeholder emitted by specialised parsers when a time/order relationship is detected but cannot be resolved to a stronger edge type.

### DEPENDS_ON_CONFIG
A binding from code to externalised configuration, emitted by the Spring enrichment for `@ConfigurationProperties` classes and the `ConfigProperty` nodes parsed out of `application.properties` / `application.yml`.

### HANDLES
A handler relationship between a dispatch point and the method that services it — Spring request mappings binding an `Endpoint` node to its controller method, and `@EventListener` methods binding to the event they consume.

### TRIGGERS
A scheduled invocation, emitted for `@Scheduled` methods to link the synthesised `Scheduler` node to the method it fires.

### PUBLISHES
An event-publication relationship, emitted where code publishes a Spring application event.

> `OVERRIDES` appears in the impact-scoring tables (`constants.py`) but is not emitted by any parser today.

## Qualified Name Format

Nodes are uniquely identified by qualified names:

```
# File node
/absolute/path/to/file.py

# Top-level function
/absolute/path/to/file.py::function_name

# Method in a class
/absolute/path/to/file.py::ClassName.method_name

# Nested class method
/absolute/path/to/file.py::OuterClass.InnerClass.method_name
```

## Presentation-layer resolver (`[resolvers.jsp]`)

The JSP/HTML resolver is **enabled by default** with framework-level defaults; a `[resolvers.jsp]` section in `.code-review-graph/config.toml` overrides individual keys, and `enabled = false` switches it off entirely. A missing or malformed config file means "run with the defaults", never an error. The resolver creates no nodes — it discovers pages (`jsp`, `html`) and scripts (`javascript`) from the graph's own File nodes and rebuilds all of its edges (`RENDERS`, `REQUESTS`, `INCLUDES`, `REFERENCES` from page sources) from live graph state on every run, so deletions never leave stale edges.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| enabled | bool | `true` | Master switch for the resolver |
| web_root | string | `"web"` | Directory absolute hrefs are probed under |
| source_root | string | `"src"` | Root of the Java source scanned for route annotations |
| route_annotations | list | `["UrlBinding", "RequestMapping"]` | Class-level annotations that declare a route |
| bean_attribute | string | `"beanclass"` | Attribute naming the rendered bean class |
| bean_package_prefix | string | `"com."` | Required prefix of a bean FQN value |
| dead_url_suffixes | list | `[".action"]` | URL suffixes that never name a live route |
| context_paths | list | `[]` | Servlet context path prefixes stripped when probing under `web_root` (e.g. `["/myapp"]`) |

## Coverage reporting

`code-review-graph coverage [--json] [--no-fail] [repo_root]` compares the repository's parseable inventory with the File nodes actually stored in the graph, so a silent indexing gap becomes one visible number. `--json` emits one machine-readable object; `--no-fail` forces exit 0 even when tracked files are missing from the graph. The same report is available over MCP as `coverage_report_tool`.

## SQLite Tables

```sql
-- Nodes table
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,
    line_start INTEGER,
    line_end INTEGER,
    language TEXT,
    parent_name TEXT,
    params TEXT,
    return_type TEXT,
    modifiers TEXT,
    is_test INTEGER DEFAULT 0,
    file_hash TEXT,
    extra TEXT DEFAULT '{}',
    community_id INTEGER,
    updated_at REAL NOT NULL
);

-- Edges table
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    source_qualified TEXT NOT NULL,
    target_qualified TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line INTEGER DEFAULT 0,
    extra TEXT DEFAULT '{}',
    confidence REAL DEFAULT 1.0,
    confidence_tier TEXT DEFAULT 'EXTRACTED',
    updated_at REAL NOT NULL
);

-- Metadata table
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Flows table (v2.0)
CREATE TABLE flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    entry_point_id INTEGER NOT NULL,
    depth INTEGER NOT NULL,
    node_count INTEGER NOT NULL,
    file_count INTEGER NOT NULL,
    criticality REAL NOT NULL DEFAULT 0.0,
    path_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Flow memberships table (v2.0)
CREATE TABLE flow_memberships (
    flow_id INTEGER NOT NULL,
    node_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (flow_id, node_id)
);

-- Communities table (v2.0)
CREATE TABLE communities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    parent_id INTEGER,
    cohesion REAL NOT NULL DEFAULT 0.0,
    size INTEGER NOT NULL DEFAULT 0,
    dominant_language TEXT,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Full-text search virtual table (v2.0)
CREATE VIRTUAL TABLE nodes_fts USING fts5(
    name, qualified_name, file_path, signature,
    content='nodes', content_rowid='rowid',
    tokenize='porter unicode61'
);

-- Token-efficient summary tables (v6)
CREATE TABLE community_summaries (
    community_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    purpose TEXT DEFAULT '',
    key_symbols TEXT DEFAULT '[]',
    risk TEXT DEFAULT 'unknown',
    size INTEGER DEFAULT 0,
    dominant_language TEXT DEFAULT ''
);

CREATE TABLE flow_snapshots (
    flow_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    entry_point TEXT NOT NULL,
    critical_path TEXT DEFAULT '[]',
    criticality REAL DEFAULT 0.0,
    node_count INTEGER DEFAULT 0,
    file_count INTEGER DEFAULT 0
);

CREATE TABLE risk_index (
    node_id INTEGER PRIMARY KEY,
    qualified_name TEXT NOT NULL,
    risk_score REAL DEFAULT 0.0,
    caller_count INTEGER DEFAULT 0,
    test_coverage TEXT DEFAULT 'unknown',
    security_relevant INTEGER DEFAULT 0,
    last_computed TEXT DEFAULT ''
);

-- Embeddings table, stored in the embeddings database
CREATE TABLE embeddings (
    qualified_name TEXT PRIMARY KEY,
    vector BLOB NOT NULL,
    text_hash TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'unknown'
);
```

Indexes include qualified-name, file-path, node-kind, edge source/target/kind, community, flow criticality, risk score, compound edge lookup indexes, and the composite edge upsert index.
