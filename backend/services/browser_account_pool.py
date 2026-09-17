"""Opt-in host Docker pool. Persistent identities/volumes, no Docker socket in agents."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.models.browser import (
    BrowserAccount,
    BrowserAccountLease,
    BrowserDurableCommand,
    BrowserLoginSession,
    BrowserRuntimeBundle,
)
from backend.models.edge_node import EdgeNode, EdgeNodeCapacity
from backend.services.browser_account_session_contract import ACTIVE_BROWSER_SESSION_STATUSES

log = logging.getLogger(__name__)


def configuration():
    name = os.environ.get("ACCOUNT_NODE_POOL_CONFIG")
    if not name:
        return None
    value = json.loads(Path(name).read_text(encoding="utf-8"))
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value["image"]):
        raise ValueError("pool requires an immutable image ID")
    if not 1 <= value["max_running"] <= 32 or not 30 <= value["idle_seconds"] <= 86400:
        raise ValueError("invalid pool resource limits")
    url = urlsplit(value["advertise_base"])
    if url.scheme != "https" or not url.netloc or url.username or url.query or url.fragment:
        raise ValueError("pool requires a trusted HTTPS endpoint")
    for key in ("state_dir", "env_template", "trust_file", "ca_file", "docker"):
        if not Path(value[key]).is_absolute():
            raise ValueError("pool paths must be absolute")
    return value


def records(config):
    path = Path(config["state_dir"]) / "pool.json"
    items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    for item in items:
        if not re.fullmatch("[0-9a-f]{32}", item["key"]):
            raise ValueError("invalid managed pool identity")
        if item["node_id"] != str(uuid.UUID(hex=item["key"])):
            raise ValueError("invalid managed node identity")
        if item.get("container_id") and not re.fullmatch("[0-9a-f]{64}", item["container_id"]):
            raise ValueError("invalid managed container identity")
    return items


def pool_node_state(node_id):
    config = configuration()
    if config:
        return next((item["phase"] for item in records(config) if item["node_id"] == node_id), None)
    return None


def pool_node_blocked(node_id):
    return pool_node_state(node_id) in {"creating", "starting", "stopping", "stopped"}


def fresh(capacity, now):
    def utc(value):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    return bool(
        capacity and capacity.valid and utc(capacity.observed_at) <= now < utc(capacity.expires_at)
    )


def movable(account, command, session, now):
    expiry = (
        command.expires_at.replace(tzinfo=UTC)
        if command.expires_at.tzinfo is None
        else command.expires_at
    )
    return bool(
        command.kind == "start_login"
        and command.status == "queued"
        and expiry > now
        and command.claimed_at is None
        and command.epoch == 0
        and command.expected_revision == account.revision
        and not account.paused
        and account.status == "opening"
        and not (
            account.profile_id
            or account.profile_version
            or account.profile_manifest_id
            or account.platform_identity
        )
        and session.command_id == command.id
        and session.status == "opening"
        and session.purpose in {"login", "browser"}
        and not any(
            (
                session.lease_id,
                session.epoch,
                session.profile_id,
                session.profile_version,
                session.instance_id,
                session.tab_id is not None,
                session.frame_id is not None,
                session.document_id,
                session.origin,
                session.closed_at,
            )
        )
    )


class DockerAccountPool:
    def __init__(self, config, session_factory=AsyncSessionLocal):
        self.config = config
        self.session_factory = session_factory
        self.items = records(config)
        self.stop_event = asyncio.Event()
        self.idle_since = {}
        self._lock_file = None

    def save(self):
        root = Path(self.config["state_dir"])
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = root / "pool.json.tmp"
        temporary.write_text(json.dumps(self.items), encoding="utf-8")
        temporary.replace(root / "pool.json")

    async def docker(self, *args):
        process = await asyncio.create_subprocess_exec(
            self.config["docker"],
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, _ = await asyncio.wait_for(process.communicate(), 45)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise RuntimeError("pool Docker operation failed")
        return out.decode().strip()

    async def inspect(self, item):
        name = "opencli-account-pool-" + item["key"]
        raw = await self.docker("inspect", name)
        info = json.loads(raw)[0]
        labels = info["Config"].get("Labels") or {}
        if (
            labels.get("opencli.pool.node") != item["node_id"]
            or labels.get("opencli.pool.root") != self.config["state_dir"]
        ):
            raise RuntimeError("container is not owned by this pool")
        if item.get("container_id") and info["Id"] != item["container_id"]:
            raise RuntimeError("managed container was replaced")
        return info

    async def create(self, command_id):
        key = uuid.uuid4().hex
        item = dict(key=key, node_id=str(uuid.UUID(hex=key)), phase="creating", demand=command_id)
        values = dict(
            line.split("=", 1)
            for line in Path(self.config["env_template"]).read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.startswith("#")
        )
        values.update(
            AGENT_NODE_ID=item["node_id"],
            AGENT_NODE_CREDENTIAL_ID=str(uuid.uuid4()),
            AGENT_NODE_CREDENTIAL=secrets.token_urlsafe(48),
            AGENT_LABEL="弹性账号浏览器 " + key[:6],
            AGENT_ADVERTISE_URL=self.config["advertise_base"].rstrip("/") + "/" + key,
        )
        env_path = Path(self.config["state_dir"]) / (key + ".env")
        with env_path.open("x", encoding="utf-8") as stream:
            os.chmod(env_path, 0o600)
            stream.write("\n".join(k + "=" + v for k, v in values.items()) + "\n")
        self.items.append(item)
        self.save()
        await self.ensure_created(item)

    async def ensure_created(self, item):
        name = "opencli-account-pool-" + item["key"]
        existing = await self.docker("ps", "-aq", "--filter", "name=^/" + name + "$")
        if existing:
            info = await self.inspect(item)
            item["container_id"] = info["Id"]
            await self.start(item)
            return
        env_path = Path(self.config["state_dir"]) / (item["key"] + ".env")
        if not env_path.is_file():
            raise RuntimeError("managed node environment missing")
        item["container_id"] = await self.docker(
            "create",
            "--init",
            "--name",
            name,
            "--label",
            "opencli.pool.node=" + item["node_id"],
            "--label",
            "opencli.pool.root=" + self.config["state_dir"],
            "--network",
            self.config["network"],
            "--env-file",
            str(env_path),
            "--shm-size",
            "1g",
            "--memory",
            "2g",
            "--cpus",
            "2",
            "--pids-limit",
            "512",
            "--mount",
            f"type=volume,src={name}-state,dst=/var/lib/opencli/account-runtime",
            "--mount",
            f"type=bind,src={self.config['trust_file']},dst=/runtime-trust/trust.pem,readonly",
            "--mount",
            f"type=bind,src={self.config['ca_file']},dst=/runtime-trust/ca.pem,readonly",
            self.config["image"],
        )
        self.save()
        await self.start(item)

    async def start(self, item):
        self.idle_since.pop(item["node_id"], None)
        info = await self.inspect(item)
        if not info["State"]["Running"]:
            async with self.session_factory() as db:
                node = await db.get(EdgeNode, item["node_id"])
                item["previous_boot"] = node.boot_id if node else None
        item["phase"] = "starting"
        self.save()
        if not info["State"]["Running"]:
            await self.docker("start", item["container_id"])

    async def bind(self, item):
        from backend.browser_login_rules import load_bundle_login_rule
        from backend.services.browser_account_login_options import _capacity_reports_rule

        async with self.session_factory() as db:
            node = await db.get(EdgeNode, item["node_id"], with_for_update=True)
            if not node or node.status != "online" or node.quarantined or not node.account_capable:
                return False
            capacity = await db.scalar(
                select(EdgeNodeCapacity)
                .where(
                    EdgeNodeCapacity.node_id == node.id, EdgeNodeCapacity.boot_id == node.boot_id
                )
                .with_for_update()
            )
            if not fresh(capacity, datetime.now(UTC)) or capacity.occupied_slots:
                return False
            command = (
                await db.get(BrowserDurableCommand, item.get("demand"), with_for_update=True)
                if item.get("demand")
                else None
            )
            if command is None or command.status != "queued":
                return True
            account = await db.get(BrowserAccount, command.account_id, with_for_update=True)
            session = await db.get(BrowserLoginSession, command.session_id, with_for_update=True)
            if (
                not account
                or not session
                or not movable(account, command, session, datetime.now(UTC))
            ):
                return True
            # Historical leases include quarantined writers; never move these accounts.
            if await db.scalar(
                select(BrowserAccountLease.lease_id)
                .where(BrowserAccountLease.account_id == account.id)
                .limit(1)
            ):
                return True
            bundle_id = account.runtime_bundle_id or self.config["bundle_id"]
            bundle = await db.get(BrowserRuntimeBundle, bundle_id)
            if not bundle or (
                account.runtime_bundle_version and account.runtime_bundle_version != bundle.version
            ):
                return False
            if not _capacity_reports_rule(
                capacity,
                bundle.id,
                bundle.version,
                account.login_rule_id,
                account.login_rule_version,
            ):
                return False
            if not load_bundle_login_rule(
                Path(__file__).resolve().parents[2],
                bundle_name=bundle.name,
                bundle_version=bundle.version,
                bundle_manifest=bundle.manifest,
                rule_id=account.login_rule_id,
                rule_version=account.login_rule_version,
            ):
                return False
            account.node_id = session.node_id = command.node_id = node.id
            account.runtime_bundle_id, account.runtime_bundle_version = bundle.id, bundle.version
            account.revision += 1
            command.expected_revision = account.revision
            session.node_boot_id = node.boot_id
            session.revision += 1
            await db.commit()
            return True

    async def idle(self, item):
        async with self.session_factory() as db:
            node = await db.get(EdgeNode, item["node_id"], with_for_update=True)
            if (
                not node
                or node.status != "online"
                or node.quarantined
                or node.boot_id != item.get("boot_id")
            ):
                return False
            cap = await db.scalar(
                select(EdgeNodeCapacity)
                .where(
                    EdgeNodeCapacity.node_id == node.id, EdgeNodeCapacity.boot_id == node.boot_id
                )
                .with_for_update()
            )
            busy = not fresh(cap, datetime.now(UTC)) or cap.occupied_slots != 0
            busy = busy or bool(
                await db.scalar(
                    select(BrowserDurableCommand.id)
                    .where(
                        BrowserDurableCommand.node_id == node.id,
                        BrowserDurableCommand.status.in_(["queued", "claimed", "running"]),
                    )
                    .limit(1)
                )
            )
            busy = busy or bool(
                await db.scalar(
                    select(BrowserAccountLease.lease_id)
                    .where(
                        BrowserAccountLease.node_id == node.id,
                        BrowserAccountLease.released_at.is_(None),
                        BrowserAccountLease.status.in_(["active", "quarantined"]),
                    )
                    .limit(1)
                )
            )
            busy = busy or bool(
                await db.scalar(
                    select(BrowserLoginSession.id)
                    .where(
                        BrowserLoginSession.node_id == node.id,
                        BrowserLoginSession.status.in_(ACTIVE_BROWSER_SESSION_STATUSES),
                    )
                    .limit(1)
                )
            )
            if busy:
                self.idle_since.pop(node.id, None)
                return False
            since = self.idle_since.setdefault(node.id, time.monotonic())
            if time.monotonic() - since < self.config["idle_seconds"]:
                return False
            # Durable pool guard is checked under the same node lock by the scheduler.
            item["phase"] = "stopping"
            self.save()
            await db.commit()
        await self.inspect(item)
        await self.docker("stop", "--time", "10", item["container_id"])
        if (await self.inspect(item))["State"]["Running"]:
            raise RuntimeError("pool container did not stop")
        item["phase"] = "stopped"
        self.save()
        return True

    async def ready_boot(self, item):
        async with self.session_factory() as db:
            node = await db.get(EdgeNode, item["node_id"])
            cap = (
                await db.scalar(
                    select(EdgeNodeCapacity).where(
                        EdgeNodeCapacity.node_id == item["node_id"],
                        EdgeNodeCapacity.boot_id == node.boot_id,
                    )
                )
                if node
                else None
            )
            if (
                node
                and not node.quarantined
                and node.status == "online"
                and fresh(cap, datetime.now(UTC))
            ):
                return node.boot_id
        return None

    async def reconcile(self, item):
        if item["phase"] == "creating":
            await self.ensure_created(item)
        elif item["phase"] == "starting":
            if not (await self.inspect(item))["State"]["Running"]:
                await self.docker("start", item["container_id"])
                return
            boot = await self.ready_boot(item)
            if (
                boot is not None
                and boot != item.get("previous_boot")
                and (await self.inspect(item))["State"]["Running"]
                and await self.bind(item)
            ):
                item.update(phase="ready", boot_id=boot, demand=None)
                self.idle_since.pop(item["node_id"], None)
                self.save()
        elif item["phase"] == "stopping":
            await self.inspect(item)
            await self.docker("stop", "--time", "10", item["container_id"])
            item["phase"] = "stopped"
            self.save()
        elif item["phase"] == "ready":
            info = await self.inspect(item)
            if not info["State"]["Running"]:
                await self.start(item)
                return
            boot = await self.ready_boot(item)
            if boot != item.get("boot_id"):
                if boot is not None and await self.bind(item):
                    item.update(boot_id=boot, demand=None)
                    self.idle_since.pop(item["node_id"], None)
                    self.save()
                return
            await self.idle(item)

    async def tick(self):
        for item in self.items:
            try:
                await self.reconcile(item)
            except Exception:
                log.error(
                    "Managed node reconciliation failed; will retry without removing its state"
                )
        async with self.session_factory() as db:
            pending = (
                await db.execute(
                    select(BrowserDurableCommand, BrowserAccount, BrowserLoginSession)
                    .join(BrowserAccount, BrowserAccount.id == BrowserDurableCommand.account_id)
                    .join(
                        BrowserLoginSession,
                        BrowserLoginSession.id == BrowserDurableCommand.session_id,
                    )
                    .where(
                        BrowserDurableCommand.status == "queued",
                        BrowserDurableCommand.kind == "start_login",
                        BrowserDurableCommand.expires_at > datetime.now(UTC),
                    )
                    .order_by(BrowserDurableCommand.available_at)
                    .limit(100)
                )
            ).all()
            demands = {item.get("demand") for item in self.items}
            for command, account, session in pending:
                own = next(
                    (item for item in self.items if item["node_id"] == account.node_id), None
                )
                if own and own["phase"] == "stopped":
                    if (
                        sum(i["phase"] != "stopped" for i in self.items)
                        < self.config["max_running"]
                    ):
                        await self.start(own)
                    continue
                if (
                    (own and own["phase"] in {"starting", "creating", "stopping"})
                    or command.id in demands
                    or not movable(account, command, session, datetime.now(UTC))
                ):
                    continue
                if await db.scalar(
                    select(BrowserAccountLease.lease_id)
                    .where(BrowserAccountLease.account_id == account.id)
                    .limit(1)
                ):
                    continue
                capacity = await db.scalar(
                    select(EdgeNodeCapacity)
                    .join(EdgeNode, EdgeNode.id == EdgeNodeCapacity.node_id)
                    .where(
                        EdgeNode.id == account.node_id,
                        EdgeNode.status == "online",
                        EdgeNodeCapacity.boot_id == EdgeNode.boot_id,
                    )
                )
                if (
                    fresh(capacity, datetime.now(UTC))
                    and capacity.occupied_slots < capacity.slot_limit
                ):
                    continue  # Let normal dispatch claim the existing free node.
                if sum(i["phase"] != "stopped" for i in self.items) >= self.config["max_running"]:
                    break
                await self.create(command.id)
                break  # Bounded rate: at most one new container per tick.

    async def run(self):
        root = Path(self.config["state_dir"])
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock_file = (root / "controller.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._lock_file.seek(0)
                self._lock_file.write(b"0")
                self._lock_file.flush()
                self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            while not self.stop_event.is_set():
                try:
                    await self.tick()
                except Exception:
                    log.error("Account pool reconciliation failed; identities and volumes retained")
                try:
                    await asyncio.wait_for(self.stop_event.wait(), 5)
                except TimeoutError:
                    pass
        finally:
            self._lock_file.close()
