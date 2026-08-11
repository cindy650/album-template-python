__all__ = [
    "DeepSeekProductInformationTranslator",
    "OrderPrintImageGenerator",
    "OrderRepository",
    "OrderService",
]


def __getattr__(name):
    if name == "OrderRepository":
        from backend.orders.repository import OrderRepository

        return OrderRepository
    if name == "OrderService":
        from backend.orders.service import OrderService

        return OrderService
    if name in {
        "DeepSeekProductInformationTranslator",
        "OrderPrintImageGenerator",
    }:
        from backend.orders.print_image import (
            DeepSeekProductInformationTranslator,
            OrderPrintImageGenerator,
        )

        return {
            "DeepSeekProductInformationTranslator": (
                DeepSeekProductInformationTranslator
            ),
            "OrderPrintImageGenerator": OrderPrintImageGenerator,
        }[name]
    raise AttributeError(name)
