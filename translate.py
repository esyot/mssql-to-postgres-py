#!/usr/bin/env python3
from __future__ import annotations

import argparse
import codecs
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

I = re.I
S = re.S

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
PG_RESERVED = frozenset("""
all analyse analyze and any array as asc asymmetric both case cast check collate
column constraint create current_catalog current_date current_role current_time
current_timestamp current_user default deferrable desc distinct do else end except
false fetch for foreign from grant group having in initially intersect into lateral
leading limit localtime localtimestamp not null offset on only or order placing
primary references returning select session_user some symmetric table then to
trailing true union unique user using variadic when where window with
""".split())

IDENT = r"(?:\x00\d+\x00|[A-Za-z_@#][\w$@#]*)"
QNAME = IDENT + r"(?:\s*\.\s*" + IDENT + r")*"
PH_RE = re.compile(r"\x00(\d+)\x00")
STRUCT = re.compile(r"[(),]")

# Masking tokenizer (unrolled loops: fast on multi-MB literals)
TOKEN_RE = re.compile(
    r"(?P<lc>--[^\n]*)"
    r"|(?P<bc>/\*.*?\*/)"
    r"|(?P<str>(?:(?<![\w$@#])[Nn])?'[^']*(?:''[^']*)*')"
    r"|(?P<br>\[[^\]]*(?:\]\][^\]]*)*\])"
    r"|(?P<dq>\"[^\"]*(?:\"\"[^\"]*)*\")",
    S,
)

SET_OPT = (
    r"SET\s+(?:ANSI_\w+|QUOTED_IDENTIFIER|NOCOUNT|XACT_ABORT|ARITHABORT|"
    r"NUMERIC_ROUNDABORT|CONCAT_NULL_YIELDS_NULL|IDENTITY_INSERT|DATEFORMAT|"
    r"LANGUAGE|TRANSACTION|IMPLICIT_TRANSACTIONS)\b"
)
SET_OPT_RE = re.compile(SET_OPT, I)
STMT_SPLIT = re.compile(
    r"(?im)^(?=\s*(?:INSERT|CREATE|ALTER|DROP|UPDATE|DELETE|TRUNCATE|EXEC(?:UTE)?|USE)\b"
    r"|\s*" + SET_OPT + r")"
)
GO_RE = re.compile(r"^\s*GO(?:\s+\d+)?\s*(?:--.*)?$", I)

# Removed DATEPART| to prevent false warnings
NEEDS_REVIEW_RE = re.compile(
    r"\b(DATEADD|DATEDIFF|DATENAME|CHARINDEX|STUFF|IIF|FORMAT|TOP|OUTPUT|"
    r"APPLY|PIVOT|UNPIVOT|ROWCOUNT|RAISERROR|PRINT|DECLARE|CURSOR|NOLOCK|STRING_AGG)\b|@@\w+",
    I,
)

DATEPART_RE = re.compile(
    r"\bDATEPART\s*\(\s*(?P<p>[a-z_]+)\s*,\s*(?P<e>[^,()]+(?:\([^()]*\))?)\s*\)", I
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def split_top(s: str, sep: str = ",") -> list[str]:
    parts, depth, last = [], 0, 0
    for m in re.finditer(r"[()]|" + re.escape(sep), s):
        c = m.group()
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0:
            parts.append(s[last:m.start()])
            last = m.end()
    parts.append(s[last:])
    return parts


def matching_paren(s: str, open_i: int) -> int:
    depth = 0
    for m in re.finditer(r"[()]", s[open_i:]):
        depth += 1 if m.group() == "(" else -1
        if depth == 0:
            return open_i + m.start()
    return -1


def fix_bits(values: str, bit_idx: set[int]) -> str:
    """Turn 1/0 into TRUE/FALSE at the given column positions of VALUES (...),(...)."""
    edits, depth, fld, start = [], 0, 0, 0
    for m in STRUCT.finditer(values):
        c = m.group()
        if c == "(":
            depth += 1
            if depth == 1:
                fld, start = 0, m.end()
        elif c == ")":
            if depth == 1 and fld in bit_idx:
                edits.append((start, m.start()))
            depth -= 1
        elif depth == 1:  # comma
            if fld in bit_idx:
                edits.append((start, m.start()))
            fld += 1
            start = m.end()
    for a, b in reversed(edits):
        t = values[a:b].strip()
        if t in ("0", "1"):
            values = values[:a] + values[a:b].replace(t, "TRUE" if t == "1" else "FALSE") + values[b:]
    return values


# SSMS scripts datetimes as CAST(0x... AS DateTime); decode them to real literals.
DT_HEX_RE = re.compile(r"\bCAST\s*\(\s*0x([0-9A-Fa-f]+)\s+AS\s+(DateTime|Date|SmallDateTime)\s*\)", I)
HEX_RE = re.compile(r"\b0[xX]([0-9A-Fa-f]*)\b")


def _dt_cb(m: re.Match) -> str:
    try:
        raw, kind = bytes.fromhex(m.group(1)), m.group(2).lower()
        if kind == "datetime" and len(raw) == 8:
            days = int.from_bytes(raw[:4], "big", signed=True)
            ticks = int.from_bytes(raw[4:], "big")  # 1/300 s
            dt = datetime(1900, 1, 1) + timedelta(days=days, milliseconds=round(ticks * 10 / 3))
            return "'%s'::timestamp" % dt.isoformat(sep=" ", timespec="milliseconds")
        if kind == "smalldatetime" and len(raw) == 4:
            dt = datetime(1900, 1, 1) + timedelta(
                days=int.from_bytes(raw[:2], "big"), minutes=int.from_bytes(raw[2:], "big"))
            return "'%s'::timestamp" % dt.isoformat(sep=" ", timespec="seconds")
        if kind == "date" and len(raw) == 3:
            d = date(1, 1, 1) + timedelta(days=int.from_bytes(raw, "little"))
            return "'%s'::date" % d.isoformat()
    except (ValueError, OverflowError):
        pass
    return m.group(0)


def decode_hex(s: str) -> str:
    s = DT_HEX_RE.sub(_dt_cb, s)
    return HEX_RE.sub(lambda m: "'\\x%s'::bytea" % m.group(1), s)


def detect_encoding(path: str) -> str:
    with open(path, "rb") as f:
        head = f.read(4)
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"  # SSMS "Unicode" default; python consumes the BOM
    if b"\x00" in head:
        return "utf-16-le" if head[1:2] == b"\x00" else "utf-16-be"
    return "utf-8"


def iter_batches(fh):
    """Yield (first_line_no, text) for each GO-terminated batch."""
    buf, start = [], 1
    for n, line in enumerate(fh, 1):
        if GO_RE.match(line):
            if buf:
                text = "".join(buf)
                # a GO line inside an open string literal is data, not a separator
                if text.count("'") % 2 and "'" in TOKEN_RE.sub("", text):
                    buf.append(line)
                    continue
                yield start, text
            buf, start = [], n + 1
        else:
            # safety valve for dumps without GO: cut at an INSERT boundary
            if len(buf) >= 5000 and line[:6].upper() == "INSERT":
                yield start, "".join(buf)
                buf, start = [], n
            buf.append(line)
    if buf:
        yield start, "".join(buf)


# --------------------------------------------------------------------------- #
# Masking
# --------------------------------------------------------------------------- #
class Masker:
    """Replaces strings/identifiers by \\x00N\\x00 and drops comments."""

    def __init__(self, conv: "Converter"):
        self.items: list[tuple[str, str | None]] = []  # (rendered, raw_ident or None)
        self.conv = conv

    def mask(self, text: str) -> str:
        items, render = self.items, self.conv.render_ident

        def repl(m: re.Match) -> str:
            kind, tok = m.lastgroup, m.group()
            if kind == "lc":
                return ""
            if kind == "bc":
                return " "
            if kind == "str":
                items.append((tok[1:] if tok[0] in "Nn" else tok, None))
            elif kind == "br":
                raw = tok[1:-1].replace("]]", "]")
                items.append((render(raw), raw))
            else:
                raw = tok[1:-1].replace('""', '"')
                items.append((render(raw), raw))
            return "\x00%d\x00" % (len(items) - 1)

        return TOKEN_RE.sub(repl, text)

    def unmask(self, s: str) -> str:
        return PH_RE.sub(lambda m: self.items[int(m.group(1))][0], s)

    def raw(self, ph: str) -> str | None:
        m = PH_RE.fullmatch(ph.strip())
        return self.items[int(m.group(1))][1] if m else None


# --------------------------------------------------------------------------- #
# Converter
# --------------------------------------------------------------------------- #
class TableInfo:
    def __init__(self):
        self.bits: set[str] = set()
        self.identity: list[tuple[str, str]] = []  # (raw, rendered)


COL_RE = re.compile(
    r"(?P<col>" + IDENT + r")\s+(?P<type>" + IDENT + r")"
    r"(?P<args>\s*\(\s*(?:max|\d+)(?:\s*,\s*\d+)?\s*\))?(?P<rest>.*)$", I | S)
CONSTR_RE = re.compile(r"(?:CONSTRAINT\s+" + IDENT + r"\s+)?(?:PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK)\b", I)
IDENTITY_RE = re.compile(r"\bIDENTITY\s*(?:\(\s*(\d+)\s*,\s*(\d+)\s*\))?", I)
BIT_DEFAULT_RE = re.compile(r"\bDEFAULT\s*\(*\s*([01])\s*\)*", I)
DBL_PAREN_DEFAULT_RE = re.compile(r"\bDEFAULT\s*\(\s*\(([^()]*)\)\s*\)", I)
NAMED_DEFAULT_RE = re.compile(r"\bCONSTRAINT\s+" + IDENT + r"\s+(?=DEFAULT\b)", I)

DDL_CLEAN = [
    (re.compile(r"\bWITH\s*\(\s*[A-Za-z_]+\s*=[^()]*(?:\([^()]*\)[^()]*)*\)", I), ""),
    (re.compile(r"\b(?:TEXTIMAGE_ON|FILESTREAM_ON)\s+(?:\x00\d+\x00|\w+)", I), ""),
    (re.compile(r"(?<=\))\s*\bON\s+(?:\x00\d+\x00|PRIMARY|DEFAULT)(?!\s*\()", I), ""),
    (re.compile(r"\b(?:NON)?CLUSTERED\b(?!\s+COLUMNSTORE)", I), ""),
    (re.compile(r"\bNOT\s+FOR\s+REPLICATION\b", I), ""),
    (re.compile(r"\b(?:ROWGUIDCOL|SPARSE|PERSISTED)\b", I), ""),
    (re.compile(r"\bCOLLATE\s+\w+", I), ""),
    (re.compile(r"\bASC\b", I), ""),
]

FUNC_SUBS = [
    (re.compile(r"\b(?:GETDATE|SYSDATETIME)\s*\(\s*\)", I), "NOW()"),
    (re.compile(r"\b(?:GETUTCDATE|SYSUTCDATETIME)\s*\(\s*\)", I), "(NOW() AT TIME ZONE 'UTC')"),
    (re.compile(r"\b(?:NEWID|NEWSEQUENTIALID)\s*\(\s*\)", I), "gen_random_uuid()"),
    (re.compile(r"\bISNULL\s*\(", I), "COALESCE("),
    (re.compile(r"\bLEN\s*\(", I), "LENGTH("),
    (re.compile(r"\bDATALENGTH\s*\(", I), "OCTET_LENGTH("),
    (re.compile(r"\bDB_NAME\s*\(\s*\)", I), "current_database()"),
]
ARGS = r"(?:\s*\(\s*(?:max|\d+)(?:\s*,\s*\d+)?\s*\))?"
CAST_TYPE_RE = re.compile(r"\bAS\s+(?P<t>\x00\d+\x00|[A-Za-z_]+)(?P<a>" + ARGS + r")(?=\s*\))", I)
CONVERT_RE = re.compile(
    r"\bCONVERT\s*\(\s*(?P<t>\x00\d+\x00|[A-Za-z_]+)(?P<a>" + ARGS + r")\s*,\s*"
    r"(?P<e>[^,()]+(?:\([^()]*\))?)\s*(?P<st>,\s*\d+\s*)?\)", I)


class Converter:
    def __init__(self, preserve_case=False, schema=None, defer_fks=True):
        self.preserve_case = preserve_case
        self.schema = schema
        self.tables: dict[str, TableInfo] = {}
        self.pre: list[str] = []
        self.idx: list[str] = []
        self.fks: list[str] = self.idx if not defer_fks else []
        self.post: list[str] = []
        self.todo: list[str] = []
        self.data_fh = None
        self.used_index_names: set[str] = set()
        self.stats: Counter = Counter()
        self.warnings: Counter = Counter()
        self.warn_line: dict[str, int] = {}
        self.cur_line = 0
        self.m: Masker = Masker(self)

    # ---- bookkeeping ------------------------------------------------------ #
    def warn(self, msg: str) -> None:
        self.warnings[msg] += 1
        self.warn_line.setdefault(msg, self.cur_line)

    def render_ident(self, raw: str) -> str:
        name = raw if self.preserve_case else raw.lower()
        if len(name.encode("utf-8")) > 63:
            self.warn("identifier longer than 63 bytes (PostgreSQL truncates it)")
        if re.fullmatch(r"[a-z_][a-z0-9_$]*", name) and name not in PG_RESERVED:
            return name
        return '"' + name.replace('"', '""') + '"'

    def tok(self, t: str) -> str:
        t = t.strip()
        m = PH_RE.fullmatch(t)
        return self.m.items[int(m.group(1))][0] if m else self.render_ident(t.lower())

    def plain(self, t: str) -> str:
        raw = self.m.raw(t)
        if raw is not None:
            return raw if self.preserve_case else raw.lower()
        return t.strip().lower()

    def qname(self, t: str) -> str:
        return ".".join(self.tok(p) for p in re.split(r"\s*\.\s*", t.strip()))

    def type_name(self, t: str) -> str:
        raw = self.m.raw(t)
        return raw if raw is not None else t

    def emit(self, bucket: list[str], skeleton: str, keep_newlines: bool = False) -> None:
        text = self.m.unmask(skeleton if keep_newlines else " ".join(skeleton.split()))
        bucket.append(text.rstrip().rstrip(";") + ";")

    def unconverted(self, skeleton: str, reason: str, bucket: list[str] | None = None) -> None:
        self.warn(reason)
        text = self.m.unmask(skeleton).strip()
        block = "-- [UNCONVERTED] " + reason + "\n" + "\n".join("-- " + ln for ln in text.splitlines())
        (self.todo if bucket is None else bucket).append(block)

    # ---- type mapping ----------------------------------------------------- #
    def map_type(self, name: str, args: str = "") -> str | None:
        t = name.lower()
        nums = re.findall(r"\d+|max", args, I)
        n = nums[0].lower() if nums else None
        if t in ("varchar", "nvarchar"):
            return "text" if n == "max" else (f"varchar({n})" if n else "varchar")
        if t in ("char", "nchar"):
            return "text" if n == "max" else f"char({n or 1})"
        if t in ("text", "ntext", "sql_variant"):
            return "text"
        if t in ("int", "integer"):
            return "integer"
        if t in ("bigint", "smallint"):
            return t
        if t == "tinyint":
            return "smallint"
        if t == "bit":
            return "boolean"
        if t in ("decimal", "numeric", "dec"):
            return f"numeric({','.join(nums)})" if nums else "numeric(18,0)"
        if t == "money":
            return "numeric(19,4)"
        if t == "smallmoney":
            return "numeric(10,4)"
        if t == "float":
            return "real" if n and n != "max" and int(n) <= 24 else "double precision"
        if t == "real":
            return "real"
        if t == "datetime":
            return "timestamp(3)"
        if t == "smalldatetime":
            return "timestamp(0)"
        if t == "datetime2":
            return f"timestamp({min(int(n), 6)})" if n else "timestamp"
        if t == "datetimeoffset":
            return f"timestamptz({min(int(n), 6)})" if n else "timestamptz"
        if t == "date":
            return "date"
        if t == "time":
            return f"time({min(int(n), 6)})" if n else "time"
        if t in ("rowversion", "timestamp", "binary", "varbinary", "image"):
            return "bytea"
        if t == "uniqueidentifier":
            return "uuid"
        if t == "xml":
            return "xml"
        return None

    # ---- expression-level transforms -------------------------------------- #
    def xform(self, s: str) -> str:
        for pat, rep in FUNC_SUBS:
            s = pat.sub(rep, s)
        if "0x" in s or "0X" in s:
            s = decode_hex(s)

        # DATEPART mapping to EXTRACT
        def datepart_cb(m):
            part = m.group("p").lower()
            part_map = {
                'yy': 'year', 'yyyy': 'year', 'qq': 'quarter', 'q': 'quarter',
                'mm': 'month', 'm': 'month', 'dy': 'doy', 'y': 'doy', 'dayofyear': 'doy',
                'dd': 'day', 'd': 'day', 'wk': 'week', 'ww': 'week',
                'dw': 'dow', 'w': 'dow', 'weekday': 'dow', 'hh': 'hour',
                'mi': 'minute', 'n': 'minute', 'ss': 'second', 's': 'second',
                'ms': 'milliseconds', 'millisecond': 'milliseconds',
                'mcs': 'microseconds', 'microsecond': 'microseconds'
            }
            mapped = part_map.get(part, part)
            return f"EXTRACT({mapped.upper()} FROM {m.group('e').strip()})"
            
        s = DATEPART_RE.sub(datepart_cb, s)

        def conv_cb(m):
            pg = self.map_type(self.type_name(m["t"]), m["a"] or "")
            if pg is None:
                return m.group(0)
            if m["st"]:
                self.warn("CONVERT() style argument dropped - check date formatting")
            return f"CAST({m['e'].strip()} AS {pg})"

        s = CONVERT_RE.sub(conv_cb, s)
        return self.map_casts(s)

    def map_casts(self, s: str) -> str:
        """CAST(x AS DateTime) -> CAST(x AS timestamp(3)), etc."""
        def cast_cb(m):
            pg = self.map_type(self.type_name(m["t"]), m["a"] or "")
            return m.group(0) if pg is None else f"AS {pg}"
        return CAST_TYPE_RE.sub(cast_cb, s)

    def ddl_clean(self, s: str) -> str:
        for pat, rep in DDL_CLEAN:
            s = pat.sub(rep, s)
        return s

    def strip_schema(self, s: str) -> str:
        prefix = f"{self.schema}." if self.schema else ""
        items = self.m.items

        def repl(m):
            raw = items[int(m.group(1))][1]
            return prefix if raw is not None and raw.lower() == "dbo" else m.group(0)

        s = re.sub(r"\x00(\d+)\x00\s*\.\s*", repl, s)
        return re.sub(r"\bdbo\s*\.\s*", prefix, s, flags=I)

    # ---- batch / statement dispatch --------------------------------------- #
    def process_batch(self, text: str, line_no: int) -> None:
        self.cur_line = line_no
        self.m = Masker(self)
        s = self.strip_schema(self.m.mask(text)).strip()
        if not s:
            return
        if re.match(r"IF\b", s, I):
            if re.search(r"FULLTEXT", self.m.unmask(s), I):
                self.stats["skipped (T-SQL only)"] += 1
            else:
                self.unconverted(s, "conditional T-SQL batch (IF ...)")
            return
        if re.match(r"(?:CREATE|ALTER)\s+(?:OR\s+ALTER\s+)?(?:PROC(?:EDURE)?|FUNCTION|TRIGGER)\b", s, I):
            self.stats["procs/functions/triggers (commented out)"] += 1
            self.unconverted(s, "stored procedure / function / trigger needs manual PL/pgSQL port")
            return
        if re.match(r"(?:CREATE|ALTER)\s+(?:OR\s+ALTER\s+)?VIEW\b", s, I):
            self.do_view(s)
            return
        for stmt in STMT_SPLIT.split(s):
            stmt = stmt.strip().rstrip(";").strip()
            if stmt:
                self.process_stmt(stmt)

    def process_stmt(self, s: str) -> None:
        if SET_OPT_RE.match(s) or re.match(r"(?:USE|CREATE\s+DATABASE|ALTER\s+DATABASE)\b", s, I):
            self.stats["skipped (T-SQL only)"] += 1
        elif re.match(r"(?:CREATE|ALTER|DROP)\s+(?:USER|ROLE|LOGIN)\b", s, I):
            self.stats["skipped (T-SQL only)"] += 1
        elif re.match(r"EXEC(?:UTE)?\b", s, I):
            head = self.m.unmask(s[:200]).lower()
            if "sp_addextendedproperty" in head or "sp_fulltext" in head or "sys.sp_" in head:
                self.stats["skipped (T-SQL only)"] += 1
            else:
                self.unconverted(s, "EXEC statement")
        elif re.match(r"INSERT\b", s, I):
            self.do_insert(s)
        elif re.match(r"CREATE\s+TABLE\b", s, I):
            self.do_create_table(s)
        elif re.match(r"CREATE\s+(?:UNIQUE\s+)?(?:(?:NON)?CLUSTERED\s+)?INDEX\b", s, I):
            self.do_index(s)
        elif re.match(r"ALTER\s+TABLE\b", s, I):
            self.do_alter_table(s)
        elif re.match(r"DROP\s+TABLE\b", s, I):
            self.emit(self.pre, re.sub(r"DROP\s+TABLE\s+", "DROP TABLE IF EXISTS ", s, count=1, flags=I))
        elif re.match(r"(?:UPDATE|DELETE|TRUNCATE)\b", s, I):
            self.data_fh.write(self.m.unmask(" ".join(self.xform(s).split())).rstrip(";") + ";\n")
            self.stats["data statements"] += 1
        elif re.match(r"CREATE\s+SCHEMA\b", s, I):
            self.emit(self.pre, s)
        else:
            self.unconverted(s, "unrecognized statement")

    # ---- INSERT ------------------------------------------------------------ #
    INSERT_HEAD = re.compile(
        r"INSERT\s+(?:INTO\s+)?(?P<tbl>" + QNAME + r")\s*(?:\((?P<cols>[^()]*)\))?\s*(?P<rest>VALUES\b.*)$", I | S)

    def do_insert(self, s: str) -> None:
        m = self.INSERT_HEAD.match(s)
        if not m:
            out = self.xform(s)
        else:
            tbl = self.qname(m["tbl"])
            cols = [self.tok(c) for c in m["cols"].split(",")] if m["cols"] else None
            rest = m["rest"]
            info = self.tables.get(tbl)
            if info and info.bits and cols:
                bit_idx = {i for i, c in enumerate(cols) if c in info.bits}
                if bit_idx:
                    rest = fix_bits(rest, bit_idx)
            if "0x" in rest or "0X" in rest:
                rest = decode_hex(rest)
            rest = self.map_casts(rest)
            out = f"INSERT INTO {tbl}" + (f" ({', '.join(cols)})" if cols else "") + f" {rest}"
        self.data_fh.write(self.m.unmask(out).rstrip(";") + ";\n")
        self.stats["inserts"] += 1

    # ---- CREATE TABLE ------------------------------------------------------ #
    def do_create_table(self, s: str) -> None:
        s = self.ddl_clean(s)
        m = re.match(r"CREATE\s+TABLE\s+(?P<name>" + QNAME + r")\s*\(", s, I)
        close_i = matching_paren(s, m.end() - 1) if m else -1
        if not m or close_i < 0:
            return self.unconverted(s, "could not parse CREATE TABLE")
        name = self.qname(m["name"])
        info = self.tables[name] = TableInfo()
        items = []
        for raw in split_top(s[m.end():close_i], ","):
            raw = raw.strip()
            if raw:
                items.append(" ".join(raw.split()) if CONSTR_RE.match(raw) else self.column_def(info, raw))
        text = f"CREATE TABLE {name} (\n    " + ",\n    ".join(self.m.unmask(i) for i in items) + "\n);"
        self.pre.append(text)
        self.stats["tables"] += 1

    def column_def(self, info: TableInfo, item: str) -> str:
        m = COL_RE.match(item)
        if not m:
            self.warn("column definition not understood")
            return item
        tname = self.type_name(m["type"])
        if tname.lower() == "as":
            self.warn("computed column - convert to GENERATED ALWAYS AS (...) STORED manually")
            return item
        col, args = self.tok(m["col"]), m["args"] or ""
        pg = self.map_type(tname, args)
        if pg is None:
            self.warn(f"unmapped data type '{tname}' (left as-is)")
            pg = tname + args
        rest = NAMED_DEFAULT_RE.sub("", m["rest"])
        ident = IDENTITY_RE.search(rest)
        clause = ""
        if ident:
            rest = rest[:ident.start()] + rest[ident.end():]
            clause = "GENERATED BY DEFAULT AS IDENTITY"
            if ident.group(1) and (ident.group(1), ident.group(2)) != ("1", "1"):
                clause += f" (START WITH {ident.group(1)} INCREMENT BY {ident.group(2)})"
            if pg.startswith("numeric"):
                pg = "bigint"
            info.identity.append((self.plain(m["col"]), col))
        if tname.lower() == "bit":
            info.bits.add(col)
            rest = BIT_DEFAULT_RE.sub(lambda x: "DEFAULT " + ("TRUE" if x.group(1) == "1" else "FALSE"), rest)
        else:
            rest = DBL_PAREN_DEFAULT_RE.sub(r"DEFAULT (\1)", rest)
        rest = " ".join(self.xform(rest).split())
        return " ".join(p for p in (col, pg, clause, rest) if p)

    # ---- INDEX ------------------------------------------------------------- #
    def do_index(self, s: str) -> None:
        s = self.ddl_clean(s)
        m = re.match(r"CREATE\s+(?P<u>UNIQUE\s+)?INDEX\s+(?P<n>" + IDENT + r")\s+ON\s+(?P<t>" + QNAME + r")\s*\(", s, I)
        close_i = matching_paren(s, m.end() - 1) if m else -1
        if not m or close_i < 0:
            return self.unconverted(s, "unsupported index type (columnstore/xml/spatial?)", self.idx)
        table = self.qname(m["t"])
        name = self.tok(m["n"])
        if name in self.used_index_names:  # index names are per-table in T-SQL, per-schema in PG
            tpart = "_".join(self.plain(p) for p in re.split(r"\s*\.\s*", m["t"].strip()))
            name = self.render_ident(f"{tpart}_{self.plain(m['n'])}")
        self.used_index_names.add(name)
        cols = ", ".join(c.strip() for c in split_top(s[m.end():close_i]))
        tail = self.xform(s[close_i + 1:]).strip()
        text = f"CREATE {'UNIQUE ' if m['u'] else ''}INDEX {name} ON {table} ({cols})" + (f" {tail}" if tail else "")
        self.emit(self.idx, text)
        self.stats["indexes"] += 1

    # ---- ALTER TABLE ------------------------------------------------------- #
    def do_alter_table(self, s: str) -> None:
        s = self.ddl_clean(s)
        m = re.match(r"ALTER\s+TABLE\s+(?P<t>" + QNAME + r")\s+(?:WITH\s+(?P<nc>NO)?CHECK\s+)?(?P<rest>.*)$", s, I | S)
        if not m:
            return self.unconverted(s, "could not parse ALTER TABLE")
        table, rest, nocheck = self.qname(m["t"]), m["rest"].strip(), bool(m["nc"])
        info = self.tables.get(table) or TableInfo()
        prefix = f"ALTER TABLE {table}"

        if re.match(r"(?:NO)?CHECK\s+CONSTRAINT\b", rest, I):
            self.stats["skipped (T-SQL only)"] += 1
        elif (d := re.match(r"ADD\s+(?:CONSTRAINT\s+" + IDENT + r"\s+)?DEFAULT\s+(?P<e>.+?)\s+FOR\s+(?P<c>" + IDENT + r")$", rest, I | S)):
            col, expr = self.tok(d["c"]), d["e"].strip()
            b = re.fullmatch(r"\(*\s*([01])\s*\)*", expr)
            expr = ("TRUE" if b.group(1) == "1" else "FALSE") if b and col in info.bits else self.xform(expr)
            self.emit(self.pre, f"{prefix} ALTER COLUMN {col} SET DEFAULT {expr}")
        elif re.match(r"ADD\s+(?:CONSTRAINT\s+" + IDENT + r"\s+)?FOREIGN\s+KEY\b", rest, I):
            self.emit(self.fks, f"{prefix} {rest}" + (" NOT VALID" if nocheck else ""))
        elif re.match(r"ADD\s+(?:CONSTRAINT\s+" + IDENT + r"\s+)?(?:PRIMARY\s+KEY|UNIQUE|CHECK)\b", rest, I):
            self.emit(self.idx, f"{prefix} {self.xform(rest)}")
        elif re.match(r"ADD\s+(?!CONSTRAINT\b)", rest, I):
            definition = re.sub(r"^ADD\s+(?:COLUMN\s+)?", "", rest, flags=I)
            self.emit(self.pre, f"{prefix} ADD COLUMN {self.m.unmask(self.column_def(info, definition))}")
        elif (a := re.match(r"ALTER\s+COLUMN\s+(?P<c>" + IDENT + r")\s+(?P<t>" + IDENT + r")(?P<a>" + ARGS + r")\s*(?P<nn>NOT\s+NULL|NULL)?\s*$", rest, I)):
            col = self.tok(a["c"])
            pg = self.map_type(self.type_name(a["t"]), a["a"] or "")
            if pg is None:
                return self.unconverted(s, "ALTER COLUMN with unmapped type", self.pre)
            parts = [f"ALTER COLUMN {col} TYPE {pg}"]
            if a["nn"]:
                parts.append(f"ALTER COLUMN {col} {'SET' if a['nn'].upper().startswith('NOT') else 'DROP'} NOT NULL")
            self.emit(self.pre, f"{prefix} " + ", ".join(parts))
        elif re.match(r"DROP\b", rest, I):
            self.emit(self.pre, f"{prefix} {rest}")
        else:
            return self.unconverted(s, "unsupported ALTER TABLE form", self.pre)
        self.stats["alter table"] += 1

    # ---- VIEW -------------------------------------------------------------- #
    def do_view(self, s: str) -> None:
        s = re.sub(r"^ALTER\s+VIEW", "CREATE OR REPLACE VIEW", s, flags=I)
        s = re.sub(r"^CREATE\s+VIEW", "CREATE OR REPLACE VIEW", s, flags=I)
        s = self.xform(s)
        for w in sorted({x.group(0).upper() for x in NEEDS_REVIEW_RE.finditer(self.m.unmask(s))}):
            self.warn(f"view uses T-SQL construct {w} - review manually")
        self.emit(self.post, "-- REVIEW: view auto-converted\n" + s.strip(), keep_newlines=True)
        self.stats["views"] += 1

    # ---- output ------------------------------------------------------------ #
    def finish(self, out_path: str, data_path: str, transaction: bool) -> None:
        for tbl, info in self.tables.items():
            for raw, col in info.identity:
                t = tbl.replace("'", "''")
                self.post.append(
                    f"SELECT setval(pg_get_serial_sequence('{t}', '{raw.replace(chr(39), chr(39)*2)}'), "
                    f"COALESCE((SELECT MAX({col}) FROM {tbl}), 0) + 1, false);")

        def section(w, title, stmts):
            if stmts:
                w(f"-- ===== {title} =====\n" + "\n\n".join(stmts) + "\n\n")

        with open(out_path, "w", encoding="utf-8", newline="\n") as out:
            w = out.write
            w("-- Generated by mssql2pg.py\nSET client_encoding = 'UTF8';\n")
            if transaction:
                w("BEGIN;\n")
            w("\n")
            section(w, "Tables", self.pre)
            w("-- ===== Data =====\n")
            with open(data_path, "r", encoding="utf-8", newline="\n") as df:
                shutil.copyfileobj(df, out, 1 << 20)
            w("\n")
            section(w, "Primary keys, uniques, indexes, checks", self.idx)
            section(w, "Foreign keys", self.fks)
            section(w, "Views / sequences", self.post)
            if transaction:
                w("COMMIT;\n")
            if self.todo:
                w("\n-- ===== Needs manual porting =====\n" + "\n\n".join(self.todo) + "\n")


def translate_file(input_path: str, output_path: str, encoding: str | None = None,
                   preserve_case=False, schema=None, transaction=True, defer_fks=True) -> int:
    src = Path(input_path)
    if not src.exists():
        print(f"Error: file '{input_path}' not found.", file=sys.stderr)
        return 1
    enc = encoding or detect_encoding(input_path)
    print(f"Reading '{input_path}' ({src.stat().st_size / 1048576:.2f} MB, encoding={enc})...")
    
    out_dir = Path(output_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    conv = Converter(preserve_case, schema, defer_fks)
    t0 = time.time()
    
    # Safely scope the temporary file deletion in a try-finally block
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", newline="\n",
                                     suffix=".data.tmp", dir=str(out_dir or ".")) as tmp:
        conv.data_fh = tmp
        with open(input_path, "r", encoding=enc, errors="replace") as fh:
            for line_no, batch in iter_batches(fh):
                conv.process_batch(batch, line_no)
        tmp_path = tmp.name
        
    conv.data_fh = None
    try:
        conv.finish(output_path, tmp_path, transaction)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    print(f"Done in {time.time() - t0:.1f}s -> {output_path}")
    for k, v in sorted(conv.stats.items()):
        print(f"  {k}: {v}")
    if conv.warnings:
        print("\nWarnings (review these):")
        for msg, n in conv.warnings.most_common():
            print(f"  [{n}x] {msg}  (first near input line {conv.warn_line[msg]})")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Convert a SQL Server script dump to PostgreSQL.")
    ap.add_argument("input", nargs="?", default="sqls/bwui_tubsplus.sql")
    ap.add_argument("output", nargs="?", default="exports/bwui_tubsplus_postgres.sql")
    ap.add_argument("--encoding", help="force input encoding (default: auto-detect BOM / UTF-16)")
    ap.add_argument("--preserve-case", action="store_true", help='keep original identifier case (quoted "Like This")')
    ap.add_argument("--schema", help="emit this schema instead of dropping dbo (e.g. public)")
    ap.add_argument("--no-transaction", action="store_true", help="don't wrap output in BEGIN/COMMIT")
    ap.add_argument("--no-defer-fks", action="store_true", help="keep FKs in file order instead of after the data")
    a = ap.parse_args()
    sys.exit(translate_file(a.input, a.output, a.encoding, a.preserve_case, a.schema,
                            not a.no_transaction, not a.no_defer_fks))