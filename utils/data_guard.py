"""Cooperative write barrier for consistent exports across both shop services."""
import asyncio
import contextvars
import functools
import threading
import time
import uuid
from contextlib import contextmanager, asynccontextmanager
from datetime import datetime, timezone, timedelta

from pymongo import ReturnDocument
from utils.update_context import record_timing

WRITE_METHODS = {"insert_one", "insert_many", "update_one", "update_many", "replace_one", "delete_one", "delete_many",
                 "find_one_and_update", "find_one_and_replace", "find_one_and_delete", "bulk_write", "drop", "create_index", "create_indexes", "drop_index", "drop_indexes"}
_async_unit = contextvars.ContextVar("shop_lazy_write_unit", default=None)
_local = threading.local()
_async_owner = contextvars.ContextVar("shop_write_unit", default=None)


def utcnow(): return datetime.now(timezone.utc)


def as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def raw_database(db):
    return db.raw if isinstance(db, (GuardedDatabase, AsyncGuardedDatabase)) else db


def _control(db): return raw_database(db)["operational_control"]


class DatabaseWritesPaused(RuntimeError):
    pass


@contextmanager
def sync_update_unit(db, wait=False):
    """Start one request lease lazily, on its first database mutation."""
    previous = getattr(_local, 'unit', None)
    if previous is not None:
        yield
        return
    unit = {'guard': None, 'db': raw_database(db), 'wait': wait}
    _local.unit = unit
    try:
        yield
    finally:
        _local.unit = previous
        if unit['guard'] is not None:
            unit['guard'].__exit__(None, None, None)


@contextmanager
def work_guard(db, wait=True, *, _use_unit=True):
    if getattr(_local, "active", False):
        lease = _local.lease
        if lease["lost"] or utcnow() - lease["touched"] > timedelta(minutes=2):
            raise RuntimeError("Write lease heartbeat stalled; operation requires review")
        yield
        return
    unit = getattr(_local, 'unit', None)
    if _use_unit and unit is not None and unit['db'] is raw_database(db):
        if unit['guard'] is None:
            guard = work_guard(db, wait=unit['wait'], _use_unit=False)
            guard.__enter__()
            unit['guard'] = guard
        with work_guard(db, _use_unit=False):
            yield
        return
    control = _control(db)
    token = uuid.uuid4().hex
    initialized = False
    while not control.find_one_and_update({"_id": "writes", "$or": [{"freeze_until": {"$exists": False}}, {"freeze_until": {"$lte": utcnow()}}]},
            {"$set": {"active." + token: utcnow()}}, return_document=ReturnDocument.AFTER):
        if not initialized:
            control.update_one({"_id": "writes"}, {"$setOnInsert": {"active": {}}}, upsert=True)
            initialized = True
            continue
        if not wait: raise DatabaseWritesPaused("Database writes paused for a scheduled backup")
        time.sleep(0.25)
    _local.active = True
    lease = {"touched": utcnow(), "lost": False}
    _local.lease = lease
    stopped = threading.Event()
    def heartbeat():
        while not stopped.wait(20):
            try:
                touched = utcnow()
                result = control.update_one({"_id": "writes", "active." + token: {"$exists": True}}, {"$set": {"active." + token: touched}})
                lease.update(touched=touched, lost=not bool(result.matched_count))
            except Exception:
                pass  # Nested writes stop before this lease can be considered stale.
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        _local.active = False
        stopped.set()
        thread.join(timeout=1)
        control.update_one({"_id": "writes"}, {"$unset": {"active." + token: ""}})


@asynccontextmanager
async def async_work_guard(db, *, _use_unit=True):
    task = asyncio.current_task()
    current = _async_owner.get()
    if current and current[0] is task:
        lease = current[1]
        if lease["lost"] or utcnow() - lease["touched"] > timedelta(minutes=2):
            raise RuntimeError("Write lease heartbeat stalled; operation requires review")
        yield
        return
    unit = _async_unit.get()
    if _use_unit and unit is not None and unit['task'] is task:
        if unit['guard'] is None:
            guard = async_work_guard(db, _use_unit=False)
            await guard.__aenter__()
            unit['guard'] = guard
        async with async_work_guard(db, _use_unit=False):
            yield
        return
    started = time.monotonic()
    control = _control(db)
    token = uuid.uuid4().hex
    initialized = False
    while not await control.find_one_and_update({"_id": "writes", "$or": [{"freeze_until": {"$exists": False}}, {"freeze_until": {"$lte": utcnow()}}]},
            {"$set": {"active." + token: utcnow()}}, return_document=ReturnDocument.AFTER):
        if not initialized:
            await control.update_one({"_id": "writes"}, {"$setOnInsert": {"active": {}}}, upsert=True)
            initialized = True
            continue
        await asyncio.sleep(0.25)
    record_timing("backup_write_lease", time.monotonic() - started)
    lease = {"touched": utcnow(), "lost": False}
    state = _async_owner.set((task, lease))
    async def heartbeat():
        while True:
            await asyncio.sleep(20)
            try:
                touched = utcnow()
                result = await control.update_one({"_id": "writes", "active." + token: {"$exists": True}}, {"$set": {"active." + token: touched}})
                lease.update(touched=touched, lost=not bool(result.matched_count))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
    pulse = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        _async_owner.reset(state)
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)
        await control.update_one({"_id": "writes"}, {"$unset": {"active." + token: ""}})


@asynccontextmanager
async def async_update_unit(db):
    """Acquire the existing write lease at the first mutation, until update end."""
    task = asyncio.current_task()
    previous = _async_unit.get()
    if previous is not None and previous['task'] is task:
        yield
        return
    unit = {'task': task, 'guard': None}
    token = _async_unit.set(unit)
    try:
        yield
    finally:
        _async_unit.reset(token)
        if unit['guard'] is not None:
            await unit['guard'].__aexit__(None, None, None)


def guarded_async(get_database):
    def decorate(function):
        @functools.wraps(function)
        async def wrapped(*args, **kwargs):
            async with async_work_guard(get_database()):
                return await function(*args, **kwargs)
        return wrapped
    return decorate


@contextmanager
def freeze_writes(db, timeout_seconds=600):
    control = _control(db)
    control.update_one({"_id": "writes"}, {"$setOnInsert": {"active": {}}}, upsert=True)
    token = uuid.uuid4().hex
    expires = utcnow() + timedelta(seconds=timeout_seconds)
    claimed = control.find_one_and_update({"_id": "writes", "$or": [{"freeze_until": {"$exists": False}}, {"freeze_until": {"$lte": utcnow()}}]},
        {"$set": {"freeze_owner": token, "freeze_until": expires}}, return_document=ReturnDocument.AFTER)
    if not claimed: raise RuntimeError("Another consistent export is running")
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            doc = control.find_one({"_id": "writes"}) or {}
            active = doc.get("active", {})
            stale = [key for key, touched in active.items() if as_utc(touched) < utcnow() - timedelta(minutes=3)]
            if stale:
                control.update_one({"_id": "writes"}, {"$unset": {"active." + key: "" for key in stale}})
            if not any(key not in stale for key in active): break
            if time.monotonic() >= deadline: raise TimeoutError("Active operations did not drain before backup timeout")
            time.sleep(0.25)
        def ensure_valid():
            doc = control.find_one({"_id": "writes"}) or {}
            if doc.get("freeze_owner") != token or utcnow() >= expires:
                raise TimeoutError("Backup write pause expired; export discarded")
        yield ensure_valid
        ensure_valid()
    finally:
        control.update_one({"_id": "writes", "freeze_owner": token}, {"$unset": {"freeze_until": "", "freeze_owner": ""}})


class GuardedCollection:
    def __init__(self, db, collection): self.db = db; self.raw = collection
    def __getattr__(self, name):
        method = getattr(self.raw, name)
        if name not in WRITE_METHODS: return method
        def call(*args, **kwargs):
            with work_guard(self.db): return method(*args, **kwargs)
        return call


class GuardedDatabase:
    def __init__(self, database): self.raw = database
    def __getitem__(self, name):
        return self.raw[name] if name == "operational_control" else GuardedCollection(self, self.raw[name])
    def __getattr__(self, name):
        value = getattr(self.raw, name)
        return self[name] if hasattr(type(value), "insert_one") else value


class AsyncGuardedCollection:
    def __init__(self, db, collection): self.db = db; self.raw = collection
    def __getattr__(self, name):
        method = getattr(self.raw, name)
        if name not in WRITE_METHODS and name not in {'find_one', 'count_documents', 'distinct'}:
            return method
        async def call(*args, **kwargs):
            started = time.monotonic()
            try:
                if name in WRITE_METHODS:
                    async with async_work_guard(self.db):
                        return await method(*args, **kwargs)
                return await method(*args, **kwargs)
            finally:
                collection = getattr(self.raw, 'name', 'collection')
                if not isinstance(collection, str): collection = 'collection'
                record_timing('mongodb:' + collection + '.' + name, time.monotonic() - started)
        return call


class AsyncGuardedDatabase:
    def __init__(self, database): self.raw = database
    def __getitem__(self, name):
        return self.raw[name] if name == "operational_control" else AsyncGuardedCollection(self, self.raw[name])
    def __getattr__(self, name):
        value = getattr(self.raw, name)
        return self[name] if hasattr(type(value), "insert_one") else value
