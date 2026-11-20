"""Compatibility API using the typed command adapter."""
from typing import Any

from pozor.commands import handle_telegram_update
from pozor.infra.sqlite.delivery import cancel_turn, guard, health_loop, recover_delivery

__all__ = ["DeliveryPipeline", "cancel_turn", "guard", "health_loop", "recover_delivery"]
from pozor.infra.sqlite.delivery import DeliveryPipeline as _Pipeline


class DeliveryPipeline(_Pipeline):
    def __init__(self,*args: Any,**kwargs: Any):
        super().__init__(*args,**kwargs)
        self.control_handler=handle_telegram_update
