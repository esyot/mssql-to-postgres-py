# T-SQL to PostgreSQL Migration Script (`translate.py`)

A robust Python-based utility designed to convert Microsoft SQL Server (T-SQL) database dumps and schemas into clean, PostgreSQL-compatible SQL scripts.

## Overview

The `translate.py` script automates the migration of legacy T-SQL databases to PostgreSQL. Utilizing a memory-optimized streaming architecture, it efficiently processes large SQL dump files while managing edge cases like Unicode literals, administrative commands, and proprietary system functions.

---

## Core Features

- **Memory-Optimized Streaming:** Processes large SQL dump files line-by-line or in buffered chunks to prevent excessive memory overhead during heavy database migrations.
- **Dynamic Data Type Mapping:** Automatically translates SQL Server data types (`DATETIME`, `NVARCHAR`, `IDENTITY`, etc.) to their PostgreSQL equivalents (`TIMESTAMP`, `VARCHAR`, `SERIAL`, etc.).
- **Schema Routing & Case Control:** Supports custom schema specification (`--schema`) and case preservation (`--preserve-case`).
- **Transaction & Foreign Key Management:** Automatically wraps outputs in transactions and handles foreign key deferral by default, with flags to override behavior.

---

## Command-Line Arguments

You can run the script with various flags to configure input files, encodings, and transaction wrappers:

| Argument           | Type / Default                                      | Description                                                                            |
| :----------------- | :-------------------------------------------------- | :------------------------------------------------------------------------------------- |
| `input`            | Positional (`sqls/sample_tubsplus.sql`)             | Path to the source T-SQL dump file.                                                    |
| `output`           | Positional (`exports/sample_tubsplus_postgres.sql`) | Path for the destination PostgreSQL output file.                                       |
| `--encoding`       | Optional                                            | Force input encoding (defaults to auto-detecting BOM / UTF-16).                        |
| `--preserve-case`  | Flag                                                | Keep original identifier case using quoted identifiers (e.g. `"Like This"`).           |
| `--schema`         | Optional                                            | Emit a specific schema instead of dropping `dbo` (e.g., `public`).                     |
| `--no-transaction` | Flag                                                | Do not wrap the generated output in `BEGIN` / `COMMIT` blocks.                         |
| `--no-defer-fks`   | Flag                                                | Keep foreign keys in original file order instead of deferring them after data inserts. |

---

## Usage Example

Run the migration script with default parameters or customize options via the CLI:

```bash
# Run with default paths
python translate.py

# Run with custom options
python translate.py sqls/custom_dump.sql exports/custom_postgres.sql --schema public --preserve-case
```
