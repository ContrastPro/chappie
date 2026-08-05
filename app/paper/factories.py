from typing import Protocol, runtime_checkable
from uuid import uuid4

from app.paper.exceptions import PaperTradingCycleIdFactoryError


@runtime_checkable
class PaperTradingCycleIdFactory(Protocol):
    def new_cycle_id(self) -> str: ...


class UUIDPaperTradingCycleIdFactory:
    def new_cycle_id(self) -> str:
        return str(uuid4())


def create_cycle_id(factory: PaperTradingCycleIdFactory) -> str:
    if not isinstance(factory, PaperTradingCycleIdFactory):
        raise PaperTradingCycleIdFactoryError("cycle_id_factory does not implement protocol")
    try:
        value = factory.new_cycle_id()
    except Exception as error:
        raise PaperTradingCycleIdFactoryError("paper cycle ID factory failed") from error
    if not isinstance(value, str) or not value.strip():
        raise PaperTradingCycleIdFactoryError("paper cycle ID factory returned an invalid ID")
    return value
