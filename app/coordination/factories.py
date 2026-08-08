from typing import Protocol, runtime_checkable
from uuid import uuid4


@runtime_checkable
class CoordinationIdFactory(Protocol):
    def new_coordination_id(self) -> str: ...


class UUIDCoordinationIdFactory:
    def new_coordination_id(self) -> str:
        return str(uuid4())
