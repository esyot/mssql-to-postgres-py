# T-SQL to PostgreSQL Migration Script (`translate.py`)

A robust Python-based utility designed to convert Microsoft SQL Server (T-SQL) database dumps and schemas into clean, PostgreSQL-compatible SQL scripts[cite: 2].

## Overview

The `translate.py` script automates the migration of legacy T-SQL databases to PostgreSQL[cite: 2]. Utilizing a memory-optimized streaming architecture, it efficiently processes large SQL dump files while managing edge cases like Unicode literals, administrative commands, and proprietary system functions[cite: 2].

---

## Core Features

- **Memory-Optimized Streaming:** Processes large SQL dump files line-by-line or in buffered chunks to prevent excessive memory overhead during heavy database migrations[cite: 2].
- **Dynamic Data Type Mapping:** Automatically translates SQL Server data types (`DATETIME`, `NVARCHAR`, `IDENTITY`, etc.) to their PostgreSQL equivalents (`TIMESTAMP`, `VARCHAR`, `SERIAL`, etc.)[cite: 2].
- **Syntax & Function Translation:** Rewrites T-SQL specific constructs (e.g., `DATEPART`, system procedures, square bracket identifiers `[]` to standard double quotes or lowercase identifiers)[cite: 2].
- **Unicode Literal Handling:** Properly normalizes string escapes and Unicode characters for PostgreSQL compatibility[cite: 2].
- **Administrative Command Filtering:** Filters out T-SQL specific batch headers, `GO` commands, and unsupported database options[cite: 2].

---

## Technical Architecture
