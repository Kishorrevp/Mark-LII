"""
core/devices/base.py — the device layer: JARVIS Core talks to *devices*
(this laptop, a future phone) through one interface, and never needs to know
how a device is implemented.

    JARVIS Core ──► DeviceRouter ──► DeviceAgent (interface)
                                      ├─ LaptopDevice   — IMPLEMENTED (runs tasks locally
                                      │                   through the Orchestrator)
                                      └─ PhoneDevice    — INTERFACE ONLY: reports itself
                                                          unavailable until a companion
                                                          app + transport exist (Phase 6)

Remote tasks are signed (core/devices/pairing.py) and run with
origin="remote", which makes the permission engine confirm MEDIUM-risk
actions too, not just HIGH.
"""
from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


class DeviceKind(str, enum.Enum):
    LAPTOP = "laptop"
    PHONE = "phone"


class DeviceCapability(str, enum.Enum):
    FILES = "files"
    APPS = "apps"
    BROWSER = "browser"
    MESSAGING = "messaging"
    CALLS = "calls"
    NOTIFICATIONS = "notifications"
    CAMERA = "camera"
    LOCATION = "location"
    VOICE = "voice"


@dataclass
class RemoteTask:
    request: str
    origin_device: str
    required: list = field(default_factory=list)      # DeviceCapability values
    task_id: str = ""


@dataclass
class RemoteResult:
    ok: bool
    device_id: str
    task_id: str = ""
    state: str = ""
    message: str = ""
    data: dict = field(default_factory=dict)


class DeviceUnavailable(RuntimeError):
    pass


class DeviceAgent(ABC):
    device_id: str
    kind: DeviceKind
    name: str

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def capabilities(self) -> set: ...

    @abstractmethod
    def submit(self, task: RemoteTask) -> RemoteResult: ...

    def status(self, task_id: str) -> RemoteResult:
        raise DeviceUnavailable(f"{self.name} cannot report task status.")

    def info(self) -> dict:
        return {"id": self.device_id, "kind": self.kind.value, "name": self.name,
                "available": self.is_available(), "capabilities": sorted(c.value for c in self.capabilities())}


class LaptopDevice(DeviceAgent):
    """This computer. Tasks go through the same Orchestrator (and therefore the
    same permission engine and verification) as voice requests."""

    def __init__(self, orchestrator, device_id: str = "laptop-local", name: str = "This laptop"):
        self.device_id, self.kind, self.name = device_id, DeviceKind.LAPTOP, name
        self._orch = orchestrator

    def is_available(self) -> bool:
        return self._orch is not None

    def capabilities(self) -> set:
        return {DeviceCapability.FILES, DeviceCapability.APPS, DeviceCapability.BROWSER,
                DeviceCapability.MESSAGING, DeviceCapability.VOICE, DeviceCapability.CAMERA,
                DeviceCapability.NOTIFICATIONS}

    def submit(self, task: RemoteTask) -> RemoteResult:
        origin = "voice" if task.origin_device == self.device_id else "remote"
        t = self._orch.submit(task.request, origin=origin)
        return RemoteResult(True, self.device_id, t.id, t.state.value, f"Accepted as task {t.id}.")

    def status(self, task_id: str) -> RemoteResult:
        t = self._orch.store.get(task_id)
        if t is None:
            return RemoteResult(False, self.device_id, task_id, "", "Unknown task.")
        return RemoteResult(True, self.device_id, t.id, t.state.value, self._orch.store.describe(t))


class PhoneDevice(DeviceAgent):
    """Placeholder for the future companion app (ROADMAP Phase 6).

    Deliberately NOT a fake: is_available() is False and submit() raises
    DeviceUnavailable, so nothing can route work here and believe it ran.
    What exists already: pairing + message signing (pairing.py), the
    capability vocabulary, and remote-origin permission tightening."""

    def __init__(self, device_id: str, name: str = "Phone", paired: bool = False):
        self.device_id, self.kind, self.name = device_id, DeviceKind.PHONE, name
        self.paired = paired

    def is_available(self) -> bool:
        return False

    def capabilities(self) -> set:
        return {DeviceCapability.CALLS, DeviceCapability.MESSAGING, DeviceCapability.NOTIFICATIONS,
                DeviceCapability.LOCATION, DeviceCapability.CAMERA}

    def submit(self, task: RemoteTask) -> RemoteResult:
        raise DeviceUnavailable("The phone companion app is not built yet (planned for Phase 6).")


class DeviceRouter:
    def __init__(self):
        self._devices: dict[str, DeviceAgent] = {}

    def register(self, dev: DeviceAgent) -> None:
        self._devices[dev.device_id] = dev

    def devices(self) -> list[dict]:
        return [d.info() for d in self._devices.values()]

    def pick(self, required: list | None = None, preferred: str | None = None) -> DeviceAgent:
        req = {DeviceCapability(r) if not isinstance(r, DeviceCapability) else r for r in (required or [])}
        cands = [d for d in self._devices.values() if d.is_available() and req <= d.capabilities()]
        if preferred:
            for d in cands:
                if d.device_id == preferred:
                    return d
        if not cands:
            missing = ", ".join(sorted(c.value for c in req)) or "any"
            raise DeviceUnavailable(f"No available device can do: {missing}.")
        cands.sort(key=lambda d: 0 if d.kind == DeviceKind.LAPTOP else 1)
        return cands[0]

    def route(self, task: RemoteTask, preferred: str | None = None) -> RemoteResult:
        return self.pick(task.required, preferred).submit(task)
