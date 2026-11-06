"""
Data access layer.

The repository owns every SQL statement for the Docker metadata (ports,
backups, images, plans, quotas) and for the node/VPS columns the Docker
backend needs.  It deliberately returns plain dictionaries with the *same
shape* the v10 code used, so the existing Discord panels keep rendering
without changes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from .. import config
from ..errors import PortConflict
from . import models

logger = logging.getLogger("tgdbot.database.repository")

_db_lock = threading.RLock()
_local = threading.local()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


class Repository:
    """SQLite-backed storage for the Docker infrastructure metadata."""

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        self.db_path = Path(db_path or config.DATABASE_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    # ── connections ─────────────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(_local, "conn", None)
        path = getattr(_local, "path", None)
        if conn is None or path != str(self.db_path):
            conn = _new_connection(self.db_path)
            _local.conn = conn
            _local.path = str(self.db_path)
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with _db_lock:
            conn = self._conn()
            try:
                yield conn
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        return self._conn().execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        return self._conn().execute(sql, tuple(params)).fetchone()

    # ── nodes ───────────────────────────────────────────────────────────────
    @staticmethod
    def _decode_node(row: sqlite3.Row) -> Dict[str, Any]:
        node = dict(row)
        try:
            tags = json.loads(node.get("tags") or "[]")
            if isinstance(tags, str):
                tags = json.loads(tags)
            node["tags"] = tags if isinstance(tags, list) else []
        except (TypeError, ValueError):
            node["tags"] = []
        node["is_local"] = int(node.get("is_local") or 0) == 1
        node["enabled"] = int(node.get("enabled", 1) if node.get("enabled") is not None else 1) == 1
        node["transport"] = str(node.get("transport") or ("local" if node["is_local"] else "tls"))
        node["id"] = int(node.get("id") or 0)
        return node

    def get_node(self, node_id: int) -> Optional[Dict[str, Any]]:
        row = self.query_one("SELECT * FROM nodes WHERE id = ?", (int(node_id),))
        return self._decode_node(row) if row else None

    def get_local_node(self) -> Optional[Dict[str, Any]]:
        row = self.query_one("SELECT * FROM nodes WHERE is_local = 1 ORDER BY id LIMIT 1")
        return self._decode_node(row) if row else None

    def list_nodes(self) -> List[Dict[str, Any]]:
        return [self._decode_node(row) for row in
                self.query_all("SELECT * FROM nodes ORDER BY is_local DESC, id ASC")]

    def list_enabled_nodes(self) -> List[Dict[str, Any]]:
        return [node for node in self.list_nodes() if node.get("enabled")]

    def upsert_node(self, node: Dict[str, Any]) -> int:
        with self.write() as conn:
            if node.get("id"):
                conn.execute(
                    """UPDATE nodes SET name=?, location=?, total_vps=?, max_cpu=?, max_ram_gb=?,
                           tags=?, api_key=?, url=?, is_local=?, transport=?, docker_host=?,
                           docker_tls_ca=?, docker_tls_cert=?, docker_tls_key=?, docker_tls_verify=?,
                           docker_timeout=?, enabled=?, last_updated=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (node["name"], node.get("location", "Unknown"), int(node.get("total_vps", 50)),
                     int(node.get("max_cpu", 0)), int(node.get("max_ram_gb", 0)),
                     json.dumps(node.get("tags", [])), node.get("api_key"), node.get("url"),
                     1 if node.get("is_local") else 0, node.get("transport", "local"),
                     node.get("docker_host"), node.get("docker_tls_ca"), node.get("docker_tls_cert"),
                     node.get("docker_tls_key"), 1 if node.get("docker_tls_verify", True) else 0,
                     int(node.get("docker_timeout") or config.DOCKER_API_TIMEOUT),
                     1 if node.get("enabled", True) else 0, int(node["id"])))
                return int(node["id"])
            cursor = conn.execute(
                """INSERT INTO nodes (name, location, total_vps, max_cpu, max_ram_gb, tags, api_key,
                                      url, is_local, transport, docker_host, docker_tls_ca,
                                      docker_tls_cert, docker_tls_key, docker_tls_verify,
                                      docker_timeout, enabled)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (node["name"], node.get("location", "Unknown"), int(node.get("total_vps", 50)),
                 int(node.get("max_cpu", 0)), int(node.get("max_ram_gb", 0)),
                 json.dumps(node.get("tags", [])), node.get("api_key"), node.get("url"),
                 1 if node.get("is_local") else 0, node.get("transport", "local"),
                 node.get("docker_host"), node.get("docker_tls_ca"), node.get("docker_tls_cert"),
                 node.get("docker_tls_key"), 1 if node.get("docker_tls_verify", True) else 0,
                 int(node.get("docker_timeout") or config.DOCKER_API_TIMEOUT),
                 1 if node.get("enabled", True) else 0))
            return int(cursor.lastrowid)

    def update_node(self, node_id: int, **fields: Any) -> bool:
        allowed = {
            "name", "location", "total_vps", "max_cpu", "max_ram_gb", "tags", "api_key", "url",
            "is_local", "transport", "docker_host", "docker_tls_ca", "docker_tls_cert",
            "docker_tls_key", "docker_tls_verify", "docker_timeout", "enabled", "last_check",
            "docker_version", "os_info", "cpu_info", "ram_total_mb", "ram_available_mb",
            "storage_total_gb", "storage_free_gb", "containers_running", "containers_stopped",
            "quota_method", "quota_supported",
        }
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "tags":
                value = json.dumps(value or [])
            if key in ("is_local", "enabled", "docker_tls_verify", "quota_supported"):
                value = 1 if value else 0
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return False
        params.append(int(node_id))
        with self.write() as conn:
            return conn.execute(
                f"UPDATE nodes SET {', '.join(sets)}, last_updated = CURRENT_TIMESTAMP WHERE id = ?",
                params).rowcount > 0

    def delete_node(self, node_id: int, *, delete_vps: bool = False) -> bool:
        with self.write() as conn:
            if delete_vps:
                names = [str(row["container_name"]) for row in conn.execute(
                    "SELECT container_name FROM vps WHERE node_id = ?", (int(node_id),))]
                for name in names:
                    conn.execute("DELETE FROM docker_ports WHERE container_name = ?", (name,))
                    conn.execute("DELETE FROM port_forwards WHERE vps_container = ?", (name,))
                conn.execute("DELETE FROM vps WHERE node_id = ?", (int(node_id),))
            else:
                remaining = conn.execute(
                    "SELECT COUNT(*) AS c FROM vps WHERE node_id = ?", (int(node_id),)).fetchone()["c"]
                if remaining:
                    return False
            conn.execute("DELETE FROM docker_ports WHERE node_id = ?", (int(node_id),))
            return conn.execute(
                "DELETE FROM nodes WHERE id = ? AND is_local = 0", (int(node_id),)).rowcount > 0

    def count_vps_on_node(self, node_id: int) -> int:
        row = self.query_one("SELECT COUNT(*) AS c FROM vps WHERE node_id = ?", (int(node_id),))
        return int(row["c"]) if row else 0

    def allocated_resources_on_node(self, node_id: int) -> Dict[str, float]:
        rows = self.query_all("SELECT ram, cpu FROM vps WHERE node_id = ?", (int(node_id),))
        ram_gb = 0.0
        cpu = 0.0
        for row in rows:
            try:
                ram_gb += float(str(row["ram"] or "0").upper().replace("GB", "").strip() or 0)
            except ValueError:
                pass
            try:
                cpu += float(str(row["cpu"] or 0))
            except ValueError:
                pass
        return {"ram_gb": ram_gb, "cpu": cpu}

    # ── VPS records ─────────────────────────────────────────────────────────
    DOCKER_FIELDS = (
        "backend", "docker_container_id", "docker_image", "volume_name", "ssh_host_port",
        "pid_limit", "quota_method", "quota_enforced", "quota_project_id", "provision_state",
        "restart_count", "last_health_ts",
    )

    @staticmethod
    def _decode_vps(row: sqlite3.Row) -> Dict[str, Any]:
        vps = dict(row)
        for key, fallback in (("shared_with", []), ("suspension_history", [])):
            try:
                value = json.loads(vps.get(key) or "[]")
                vps[key] = value if isinstance(value, list) else fallback
            except (TypeError, ValueError):
                vps[key] = fallback
        vps["suspended"] = bool(vps.get("suspended") or 0)
        vps["whitelisted"] = bool(vps.get("whitelisted") or 0)
        vps["is_public"] = bool(vps.get("is_public") or 0)
        vps["quota_enforced"] = bool(vps.get("quota_enforced") or 0)
        vps["os_version"] = vps.get("os_version") or config.DOCKER_DEFAULT_IMAGE
        vps["node_id"] = int(vps.get("node_id") or 1)
        vps["backend"] = str(vps.get("backend") or models.DOCKER_BACKEND)
        vps["provision_state"] = str(vps.get("provision_state") or "active")
        return vps

    def get_vps_by_container(self, container_name: str) -> Optional[Dict[str, Any]]:
        row = self.query_one("SELECT * FROM vps WHERE container_name = ?", (str(container_name),))
        return self._decode_vps(row) if row else None

    def get_vps_by_id(self, vps_id: int) -> Optional[Dict[str, Any]]:
        row = self.query_one("SELECT * FROM vps WHERE id = ?", (int(vps_id),))
        return self._decode_vps(row) if row else None

    def get_user_vps(self, user_id: Union[int, str]) -> List[Dict[str, Any]]:
        return [self._decode_vps(row) for row in self.query_all(
            "SELECT * FROM vps WHERE user_id = ? ORDER BY id ASC", (str(user_id),))]

    def list_vps(self) -> List[Dict[str, Any]]:
        return [self._decode_vps(row) for row in self.query_all("SELECT * FROM vps ORDER BY id ASC")]

    def container_names(self) -> List[str]:
        return [str(row["container_name"]) for row in
                self.query_all("SELECT DISTINCT container_name FROM vps ORDER BY container_name")]

    def next_vps_id(self) -> int:
        row = self.query_one("SELECT COALESCE(MAX(id), 0) AS highest FROM vps")
        return int(row["highest"] or 0) + 1

    def patch_vps(self, container_name: str, **fields: Any) -> bool:
        allowed = {
            "user_id", "node_id", "ram", "cpu", "storage", "config", "os_version", "status",
            "suspended", "whitelisted", "is_public", "expiration_date", "root_password",
            "shared_with", "suspension_history", "created_at", *self.DOCKER_FIELDS,
        }
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in ("shared_with", "suspension_history"):
                value = json.dumps(value or [])
            if key in ("suspended", "whitelisted", "is_public", "quota_enforced"):
                value = 1 if value else 0
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return False
        params.append(str(container_name))
        with self.write() as conn:
            return conn.execute(
                f"UPDATE vps SET {', '.join(sets)}, last_modified = CURRENT_TIMESTAMP "
                "WHERE container_name = ?", params).rowcount > 0

    def insert_vps(self, vps: Dict[str, Any]) -> int:
        columns = [
            "id", "user_id", "node_id", "container_name", "ram", "cpu", "storage", "config",
            "os_version", "status", "suspended", "whitelisted", "is_public", "created_at",
            "shared_with", "suspension_history", "expiration_date", "root_password",
            *self.DOCKER_FIELDS,
        ]
        created = vps.get("created_at")
        if isinstance(created, datetime):
            created = created.isoformat()
        values = {
            "id": vps.get("id"), "user_id": str(vps["user_id"]),
            "node_id": int(vps.get("node_id") or 1),
            "container_name": str(vps["container_name"]),
            "ram": str(vps.get("ram", "1GB")), "cpu": str(vps.get("cpu", "1")),
            "storage": str(vps.get("storage", "10GB")),
            "config": str(vps.get("config", "Custom")),
            "os_version": str(vps.get("os_version", config.DOCKER_DEFAULT_IMAGE)),
            "status": str(vps.get("status", "stopped")),
            "suspended": 1 if vps.get("suspended") else 0,
            "whitelisted": 1 if vps.get("whitelisted") else 0,
            "is_public": 1 if vps.get("is_public") else 0,
            "created_at": created or _now(),
            "shared_with": json.dumps(vps.get("shared_with") or []),
            "suspension_history": json.dumps(vps.get("suspension_history") or []),
            "expiration_date": vps.get("expiration_date"),
            "root_password": vps.get("root_password"),
            "backend": vps.get("backend", models.DOCKER_BACKEND),
            "docker_container_id": vps.get("docker_container_id"),
            "docker_image": vps.get("docker_image"),
            "volume_name": vps.get("volume_name"),
            "ssh_host_port": vps.get("ssh_host_port"),
            "pid_limit": vps.get("pid_limit"),
            "quota_method": vps.get("quota_method"),
            "quota_enforced": 1 if vps.get("quota_enforced") else 0,
            "quota_project_id": vps.get("quota_project_id"),
            "provision_state": vps.get("provision_state", "active"),
            "restart_count": int(vps.get("restart_count") or 0),
            "last_health_ts": vps.get("last_health_ts"),
        }
        with self.write() as conn:
            cursor = conn.execute(
                f"INSERT INTO vps ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                [values[column] for column in columns])
            return int(cursor.lastrowid)

    def delete_vps_row(self, container_name: str) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM port_forwards WHERE vps_container = ?", (str(container_name),))
            conn.execute("DELETE FROM usage_snapshots WHERE container = ?", (str(container_name),))
            conn.execute("DELETE FROM storage_quotas WHERE container_name = ?", (str(container_name),))
            conn.execute(
                "UPDATE docker_ports SET status = 'released', released_at = ? "
                "WHERE container_name = ? AND status != 'released'", (_now(), str(container_name)))
            conn.execute("DELETE FROM vps WHERE container_name = ?", (str(container_name),))

    # ── port ledger ─────────────────────────────────────────────────────────
    def reserve_port(self, *, node_id: int, host_ip: str, host_port: int, container_name: str,
                     container_port: int, protocol: str, purpose: str = "user",
                     user_id: Optional[Any] = None) -> int:
        try:
            with self.write() as conn:
                cursor = conn.execute(
                    """INSERT INTO docker_ports (node_id, host_ip, host_port, container_name,
                                                 container_port, protocol, purpose, status,
                                                 user_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)""",
                    (int(node_id), str(host_ip), int(host_port), str(container_name),
                     int(container_port), str(protocol), str(purpose),
                     str(user_id) if user_id is not None else None, _now()))
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise PortConflict(
                f"Port {host_port}/{protocol} on node {node_id} is already allocated",
                detail=str(exc)) from exc

    def release_port(self, reservation_id: int) -> bool:
        with self.write() as conn:
            return conn.execute(
                "UPDATE docker_ports SET status = 'released', released_at = ? "
                "WHERE id = ? AND status != 'released'",
                (_now(), int(reservation_id))).rowcount > 0

    def set_port_status(self, rows: Sequence[Dict[str, Any]], status: str) -> int:
        count = 0
        with self.write() as conn:
            for row in rows:
                count += conn.execute(
                    "UPDATE docker_ports SET status = ? WHERE id = ?",
                    (status, int(row["id"]))).rowcount
        return count

    def active_ports_for_node(self, node_id: int) -> List[Dict[str, Any]]:
        return [dict(row) for row in self.query_all(
            "SELECT * FROM docker_ports WHERE node_id = ? AND status != 'released'", (int(node_id),))]

    def active_ports_for_container(self, container_name: str,
                                   node_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM docker_ports WHERE container_name = ? AND status != 'released'"
        params: List[Any] = [str(container_name)]
        if node_id is not None:
            sql += " AND node_id = ?"
            params.append(int(node_id))
        sql += " ORDER BY container_port, protocol, host_port"
        return [dict(row) for row in self.query_all(sql, params)]

    def port_history(self, container_name: Optional[str] = None, *, node_id: Optional[int] = None,
                     limit: int = 50) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM docker_ports WHERE 1=1"
        params: List[Any] = []
        if container_name:
            sql += " AND container_name = ?"
            params.append(str(container_name))
        if node_id is not None:
            sql += " AND node_id = ?"
            params.append(int(node_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [dict(row) for row in self.query_all(sql, params)]

    def count_ports_for_user(self, user_id: Union[int, str]) -> int:
        row = self.query_one(
            "SELECT COUNT(DISTINCT host_port || '/' || protocol) AS c FROM docker_ports "
            "WHERE user_id = ? AND status != 'released' AND purpose != 'ssh'",
            (str(user_id),))
        return int(row["c"]) if row else 0

    def port_row_to_forward(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Shape a ledger row like the legacy ``port_forwards`` row the UI expects."""
        return {
            "id": int(row["id"]),
            "user_id": row.get("user_id"),
            "vps_container": row.get("container_name"),
            "vps_port": int(row["container_port"]),
            "host_port": int(row["host_port"]),
            "protocol": row.get("protocol"),
            "created_at": row.get("created_at"),
            "purpose": row.get("purpose"),
            "status": row.get("status"),
            "host_ip": row.get("host_ip"),
        }

    # ── backups ─────────────────────────────────────────────────────────────
    def add_backup(self, backup: Dict[str, Any]) -> int:
        with self.write() as conn:
            cursor = conn.execute(
                """INSERT INTO docker_backups (container_name, node_id, name, archive, size_bytes,
                                               sha256, image, configuration, note, scheduled,
                                               status, verified, verify_note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       archive=excluded.archive, size_bytes=excluded.size_bytes,
                       sha256=excluded.sha256, verified=excluded.verified,
                       verify_note=excluded.verify_note, status=excluded.status""",
                (str(backup["container_name"]), int(backup.get("node_id") or 1),
                 str(backup["name"]), str(backup.get("archive") or ""),
                 int(backup.get("size_bytes") or 0), backup.get("sha256"),
                 (backup.get("configuration") or {}).get("image"),
                 json.dumps(backup.get("configuration") or {}), str(backup.get("note") or ""),
                 1 if backup.get("scheduled") else 0, str(backup.get("status") or "complete"),
                 1 if backup.get("verified") else 0, str(backup.get("verify_note") or ""),
                 str(backup.get("created_at") or _now())))
            return int(cursor.lastrowid)

    def get_backup(self, name: str) -> Optional[Dict[str, Any]]:
        row = self.query_one("SELECT * FROM docker_backups WHERE name = ?", (str(name),))
        return self._decode_backup(row) if row else None

    def list_backups(self, container_name: Optional[str] = None,
                     limit: int = 50) -> List[Dict[str, Any]]:
        if container_name:
            rows = self.query_all(
                "SELECT * FROM docker_backups WHERE container_name = ? ORDER BY created_at DESC LIMIT ?",
                (str(container_name), int(limit)))
        else:
            rows = self.query_all(
                "SELECT * FROM docker_backups ORDER BY created_at DESC LIMIT ?", (int(limit),))
        return [self._decode_backup(row) for row in rows]

    @staticmethod
    def _decode_backup(row: sqlite3.Row) -> Dict[str, Any]:
        backup = dict(row)
        try:
            backup["configuration"] = json.loads(backup.get("configuration") or "{}")
        except (TypeError, ValueError):
            backup["configuration"] = {}
        backup["verified"] = bool(backup.get("verified") or 0)
        backup["scheduled"] = bool(backup.get("scheduled") or 0)
        return backup

    def delete_backup_row(self, name: str) -> bool:
        with self.write() as conn:
            return conn.execute("DELETE FROM docker_backups WHERE name = ?", (str(name),)).rowcount > 0

    def count_backups(self, container_name: str) -> int:
        row = self.query_one("SELECT COUNT(*) AS c FROM docker_backups WHERE container_name = ?",
                             (str(container_name),))
        return int(row["c"]) if row else 0

    # ── images ──────────────────────────────────────────────────────────────
    def list_approved_images(self) -> List[Dict[str, Any]]:
        return [dict(row) for row in self.query_all(
            "SELECT * FROM docker_images WHERE approved = 1 ORDER BY id ASC")]

    def list_images(self) -> List[Dict[str, Any]]:
        return [dict(row) for row in self.query_all("SELECT * FROM docker_images ORDER BY id ASC")]

    def add_image(self, reference: str, *, label: str = "", emoji: str = "🐳",
                  description: str = "", added_by: Optional[Any] = None,
                  approved: bool = True) -> bool:
        with self.write() as conn:
            return conn.execute(
                """INSERT INTO docker_images (reference, label, emoji, description, approved,
                                              added_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(reference) DO UPDATE SET
                       label=excluded.label, emoji=excluded.emoji,
                       description=excluded.description, approved=excluded.approved""",
                (str(reference), str(label or reference), str(emoji or "🐳"), str(description),
                 1 if approved else 0, str(added_by) if added_by is not None else None,
                 _now())).rowcount >= 0

    def remove_image(self, reference: str) -> bool:
        with self.write() as conn:
            return conn.execute("DELETE FROM docker_images WHERE reference = ?",
                                (str(reference),)).rowcount > 0

    # ── plans ───────────────────────────────────────────────────────────────
    # ── Proxmox access grants ──────────────────────────────────────────────
    def grant_proxmox(self, user_id: Any, *, granted_by: Optional[Any] = None) -> bool:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO proxmox_grants (user_id, granted_by, created_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET granted_by=excluded.granted_by""",
                (str(user_id), str(granted_by) if granted_by is not None else None, _now()))
            return True

    def revoke_proxmox(self, user_id: Any) -> bool:
        with self.write() as conn:
            return conn.execute("DELETE FROM proxmox_grants WHERE user_id = ?",
                                (str(user_id),)).rowcount > 0

    def has_proxmox_grant(self, user_id: Any) -> bool:
        if user_id is None:
            return False
        return self.query_one("SELECT 1 AS allowed FROM proxmox_grants WHERE user_id = ?",
                              (str(user_id),)) is not None

    def list_proxmox_grants(self) -> List[Dict[str, Any]]:
        return [dict(row) for row in self.query_all(
            "SELECT * FROM proxmox_grants ORDER BY created_at ASC, user_id ASC")]

    def list_plans(self, enabled_only: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM resource_plans"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY ram_mb ASC, cpu ASC"
        return [dict(row) for row in self.query_all(sql)]

    def get_plan(self, name: str) -> Optional[Dict[str, Any]]:
        row = self.query_one("SELECT * FROM resource_plans WHERE name = ?", (str(name),))
        return dict(row) if row else None

    def upsert_plan(self, plan: Dict[str, Any]) -> int:
        with self.write() as conn:
            cursor = conn.execute(
                """INSERT INTO resource_plans (name, ram_mb, cpu, storage_gb, pids, description,
                                               enabled, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       ram_mb=excluded.ram_mb, cpu=excluded.cpu, storage_gb=excluded.storage_gb,
                       pids=excluded.pids, description=excluded.description,
                       enabled=excluded.enabled""",
                (str(plan["name"]), int(plan.get("ram_mb", 1024)), float(plan.get("cpu", 1)),
                 int(plan.get("storage_gb", 10)), int(plan.get("pids", config.DOCKER_DEFAULT_PID_LIMIT)),
                 str(plan.get("description", "")), 1 if plan.get("enabled", True) else 0, _now()))
            return int(cursor.lastrowid or 0)

    def delete_plan(self, name: str) -> bool:
        with self.write() as conn:
            return conn.execute("DELETE FROM resource_plans WHERE name = ?", (str(name),)).rowcount > 0

    # ── storage quotas ──────────────────────────────────────────────────────
    def set_quota(self, container_name: str, *, node_id: int, method: str,
                  project_id: Optional[int], size_gb: float, enforced: bool,
                  detail: str = "") -> None:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO storage_quotas (container_name, node_id, method, project_id,
                                               size_gb, enforced, detail, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(container_name) DO UPDATE SET
                       node_id=excluded.node_id, method=excluded.method,
                       project_id=excluded.project_id, size_gb=excluded.size_gb,
                       enforced=excluded.enforced, detail=excluded.detail,
                       updated_at=excluded.updated_at""",
                (str(container_name), int(node_id), str(method), project_id, float(size_gb),
                 1 if enforced else 0, str(detail), _now()))

    def get_quota(self, container_name: str) -> Optional[Dict[str, Any]]:
        row = self.query_one("SELECT * FROM storage_quotas WHERE container_name = ?",
                             (str(container_name),))
        return dict(row) if row else None

    def delete_quota(self, container_name: str) -> bool:
        with self.write() as conn:
            return conn.execute("DELETE FROM storage_quotas WHERE container_name = ?",
                                (str(container_name),)).rowcount > 0

    # ── misc ────────────────────────────────────────────────────────────────
    def save_usage_snapshot(self, container: str, cpu_pct: float, ram_pct: float,
                            ram_used_mb: int, ram_total_mb: int) -> None:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO usage_snapshots (container, ts, cpu_pct, ram_pct, ram_used, ram_total)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(container), _now(), float(cpu_pct), float(ram_pct),
                 int(ram_used_mb), int(ram_total_mb)))

    def save_host_snapshot(self, cpu_pct: float, ram_pct: float, disk_pct: float,
                           vps_total: int, vps_running: int) -> None:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO host_snapshots (ts, cpu_pct, ram_pct, disk_pct, vps_total, vps_run)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (_now(), float(cpu_pct), float(ram_pct), float(disk_pct),
                 int(vps_total), int(vps_running)))

    def schema_version(self) -> Optional[int]:
        row = self.query_one("SELECT value FROM meta WHERE key = 'schema_version'")
        try:
            return int(row[0]) if row else None
        except (TypeError, ValueError):
            return None
