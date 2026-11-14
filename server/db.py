"""Database backend selector: local SQLite by default, Turso (libSQL) when configured.

TURSO_DATABASE_URL + TURSO_AUTH_TOKEN switch every store (perfdb, trainer,
repstore, accounts) onto the external database, so accounts and progress
survive server restarts. The wire protocol is libSQL's HTTP pipeline
(POST /v2/pipeline), spoken with only the standard library — the SQLite
dialect the stores were written for (CREATE TABLE, INSERT OR IGNORE,
ON CONFLICT upserts, strftime, COLLATE NOCASE) is preserved exactly and no
new pip dependency is needed.

RemoteConn connection semantics (mirrors what the stores expect from sqlite3):
  - execute() runs immediately and autocommits, EXCEPT writes issued while an
    explicit transaction is open ("BEGIN" ... commit/rollback): those are
    buffered client-side and sent as ONE pipeline batch
    (BEGIN, ...writes, COMMIT/ROLLBACK). The Turso pipeline runs every
    statement of a batch on one connection, so BEGIN/COMMIT/ROLLBACK are real
    transactions - a failed import rolls back on the server, not just here.
  - a read inside an open transaction flushes the buffered writes first (in
    the same batch, before the COMMIT) so the read sees them; those writes are
    then durable, so a later rollback cannot take them back. The failure paths
    that hit this are idempotent by construction.
  - values travel as libSQL's tagged form ({"type":"text","value":...}) in
    both directions; numbers go over the wire as strings.
  - PRAGMA foreign_keys is a no-op remotely (every delete path cleans up
    explicitly); PRAGMA table_info(<t>) is answered from the live table shape
    so the stores' tolerant migrations keep working.
  - cursors expose fetchone/fetchall/iteration/rowcount like sqlite3.
"""
import base64
import http.client
import json
import os
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit

REMOTE_URL = (os.environ.get("TURSO_DATABASE_URL") or "").strip()
REMOTE_TOKEN = (os.environ.get("TURSO_AUTH_TOKEN") or "").strip()
REMOTE_ENABLED = bool(REMOTE_URL)


class RemoteError(RuntimeError):
    pass


def connect(tag, local_path):
    """One connection object per caller (stores cache it behind their locks)."""
    if REMOTE_ENABLED:
        return RemoteConn()
    local_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(local_path), check_same_thread=False)


# ---------------------------------------------------------------- http plumbing
_tls = threading.local()


def _target():
    u = urlsplit(REMOTE_URL if "://" in REMOTE_URL else "https://" + REMOTE_URL)
    return u.scheme, u.hostname, u.port or 443


def _http():
    c = getattr(_tls, "conn", None)
    if c is None:
        scheme, host, port = _target()
        cls = http.client.HTTPConnection if scheme == "http" else http.client.HTTPSConnection
        c = cls(host, port, timeout=15)
        _tls.conn = c
    return c


def _pipeline(steps, retry=True):
    """POST a /v2/pipeline request; returns the parsed results list."""
    body = json.dumps({"requests": steps}).encode("utf-8")
    err = None
    for attempt in (0, 1) if retry else (0,):
        c = _http()
        try:
            c.request("POST", "/v2/pipeline", body=body, headers={
                "Authorization": "Bearer " + REMOTE_TOKEN,
                "Content-Type": "application/json",
            })
            resp = c.getresponse()
            data = resp.read()
            if resp.status == 401:
                raise RemoteError("remote DB rejected the auth token (HTTP 401)")
            if resp.status >= 400:
                raise RemoteError("remote DB HTTP %d: %r" % (resp.status, data[:200]))
            return json.loads(data.decode("utf-8"))
        except RemoteError as e:
            try:
                c.close()
            except Exception:
                pass
            _tls.conn = None
            raise
        except (http.client.HTTPException, OSError, ValueError) as e:
            # stale keep-alive socket or hiccup: reconnect once
            try:
                c.close()
            except Exception:
                pass
            _tls.conn = None
            err = e
            time.sleep(0.15 if attempt == 0 else 0)
    raise RemoteError("remote DB request failed: %s" % err)


# ---------------------------------------------------------------- value shapes
def _norm_cell(v):
    """Hrana returns tagged values: {"type":"integer","value":"1"}."""
    if isinstance(v, dict) and "type" in v and "value" in v:
        t, val = v.get("type"), v.get("value")
        if t == "null" or val is None:
            return None
        if t == "integer":
            try:
                return int(val)
            except (TypeError, ValueError):
                return val
        if t == "float":
            try:
                return float(val)
            except (TypeError, ValueError):
                return val
        if t == "blob":
            try:
                return base64.b64decode(val)
            except Exception:
                return val
        return val
    if isinstance(v, dict) and "type" in v and v.get("type") == "null":
        return None
    return v


def _norm_rows(rows):
    return [tuple(_norm_cell(c) for c in r) for r in (rows or [])]


def _col_names(cols):
    """The pipeline returns cols as [{"name":..,"decltype":..}]."""
    out = []
    for c in (cols or []):
        if isinstance(c, dict):
            out.append(c.get("name"))
        else:
            out.append(c)
    return out


def _rowcount(result):
    """Rows touched by a statement (UPDATE/DELETE/INSERT)."""
    for key in ("affected_row_count", "rows_written", "rows_affected"):
        v = result.get(key)
        if isinstance(v, int):
            return v
    return 0


def _enc(a):
    """Encode a Python value as libSQL's tagged Value.

    The HTTP pipeline rejects bare scalars ("expected internally tagged enum
    Value"), so every argument carries its type explicitly - and numbers go
    over the wire as strings, exactly like the server sends them back.
    """
    if a is None:
        return {"type": "null", "value": None}
    if isinstance(a, bool):
        return {"type": "integer", "value": str(int(a))}
    if isinstance(a, int):
        return {"type": "integer", "value": str(a)}
    if isinstance(a, float):
        # a float must go over the wire as a JSON number - unlike integers,
        # which Turso expects as strings
        return {"type": "float", "value": a}
    if isinstance(a, str):
        return {"type": "text", "value": a}
    if isinstance(a, (bytes, bytearray)):
        return {"type": "blob", "value": base64.b64encode(bytes(a)).decode("ascii")}
    return {"type": "text", "value": str(a)}


class RemoteCursor:
    def __init__(self, rows, cols, rowcount):
        self._rows = rows
        self._i = 0
        self.description = [(n, None, None, None, None, None, None) for n in (cols or [])]
        self.rowcount = rowcount
        self.arraysize = 1

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        r = self._rows[self._i]
        self._i += 1
        return r

    def fetchall(self):
        out = self._rows[self._i:]
        self._i = len(self._rows)
        return out

    def fetchmany(self, n=1):
        out = self._rows[self._i:self._i + max(0, int(n))]
        self._i += len(out)
        return out

    def __iter__(self):
        return iter(self._rows)

    def close(self):
        pass


_READ_RE = re.compile(r"^\s*(SELECT|WITH|VALUES|EXPLAIN)\b", re.I)
_TI_RE = re.compile(r"^\s*PRAGMA\s+TABLE_INFO\s*\(\s*['\"`]?([A-Za-z0-9_]+)['\"`]?\s*\)", re.I)


class RemoteConn:
    def __init__(self):
        self._in_txn = False
        self._pending = []   # buffered writes of the open transaction

    # -- single statement ----------------------------------------------------
    @staticmethod
    def _step(sql, params=()):
        stmt = {"sql": sql}
        if params:
            stmt["args"] = [_enc(a) for a in params]
        return {"type": "execute", "stmt": stmt}

    @staticmethod
    def _result(res):
        if not isinstance(res, dict):
            raise RemoteError("remote DB: unexpected pipeline result")
        if res.get("type") == "error":
            msg = (res.get("error") or {}).get("message") or str(res)
            raise RemoteError("remote DB: %s" % msg)
        out = res.get("response") or {}
        return out.get("result") or {}

    def _run(self, steps):
        payload = _pipeline(steps)
        results = payload.get("results") or []
        if len(results) != len(steps):
            raise RemoteError("remote DB: result count mismatch (%d != %d)"
                              % (len(results), len(steps)))
        return [self._result(r) for r in results]

    def _direct(self, sql, params):
        (result,) = self._run([self._step(sql, params)])
        rows = _norm_rows(result.get("rows"))
        rc = _rowcount(result)
        return RemoteCursor(rows, _col_names(result.get("cols")), rc)

    def _flush(self, tail="COMMIT"):
        """Send the buffered transaction as ONE batch: BEGIN ... ROLLBACK/COMMIT.

        The Turso pipeline runs every statement of a batch on one connection, so
        a real BEGIN/COMMIT/ROLLBACK inside the batch gives us genuine
        transactions - a failed import rolls back for real, not just locally.
        """
        if not self._pending:
            return
        steps = [self._step("BEGIN")]
        steps += [self._step(s, p) for (s, p) in self._pending]
        steps.append(self._step(tail))
        self._pending = []
        self._run(steps)

    # -- DBAPI-ish surface ----------------------------------------------------
    def execute(self, sql, params=()):
        s = (sql or "").strip()
        up = s[:12].upper()
        if up.startswith("PRAGMA"):
            return self._pragma(s)
        if up.startswith("BEGIN"):
            self._in_txn = True
            self._pending = []
            return RemoteCursor([], [], -1)
        if up.startswith("COMMIT") or up[:4] == "END ":
            self.commit()
            return RemoteCursor([], [], -1)
        if up.startswith("ROLLBACK"):
            self.rollback()
            return RemoteCursor([], [], -1)
        if self._in_txn and not _READ_RE.match(s):
            self._pending.append((s, params))
            return RemoteCursor([], [], -1)
        # a read inside a transaction: send the pending writes first so the read
        # sees them, and close the batch with COMMIT so the server keeps them
        if self._pending:
            steps = [self._step("BEGIN")]
            steps += [self._step(ps, pp) for (ps, pp) in self._pending]
            steps.append(self._step(s, params))
            steps.append(self._step("COMMIT"))
            self._pending = []
            results = self._run(steps)
            result = results[-2]      # the read; the last step is COMMIT
            rows = _norm_rows(result.get("rows"))
            return RemoteCursor(rows, _col_names(result.get("cols")), _rowcount(result))
        return self._direct(s, params)

    def _pragma(self, sql):
        m = _TI_RE.match(sql)
        if m:
            name = m.group(1)
            # pragma_table_info is the table-valued form of PRAGMA table_info:
            # it returns the real shape (cid, name, type, notnull, dflt, pk)
            try:
                (res,) = self._run([self._step(
                    "SELECT cid, name, type, \"notnull\", dflt_value, pk"
                    " FROM pragma_table_info(?)", (name,))])
                rows = _norm_rows(res.get("rows"))
                if rows:
                    return RemoteCursor(
                        rows, ["cid", "name", "type", "notnull", "dflt_value", "pk"], 0)
            except RemoteError:
                pass
            # fallback: derive the column names from an empty select
            (res,) = self._run([self._step("SELECT * FROM \"%s\" LIMIT 0" % name, ())])
            cols = _col_names(res.get("cols"))
            rows = [(i, c, "", 0, None, 0) for i, c in enumerate(cols)]
            return RemoteCursor(rows, ["cid", "name", "type", "notnull", "dflt_value", "pk"], 0)
        # PRAGMA foreign_keys etc.: nothing to enforce client-side; deletes
        # in the stores clean up explicitly.
        return RemoteCursor([], [], -1)

    def commit(self):
        if self._in_txn:
            self._flush("COMMIT")
            self._in_txn = False
        else:
            self._flush("COMMIT")

    def rollback(self):
        self._in_txn = False
        self._flush("ROLLBACK")

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.commit()
        return False
