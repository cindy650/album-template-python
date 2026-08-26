__all__ = [
    "DeepSeekProductInformationTranslator",
    "OrderPrintImageGenerator",
    "OrderRepository",
    "OrderService",
    "WeComOrderInfoImageGenerator",
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
        "WeComOrderInfoImageGenerator",
    }:
        from backend.orders.print_image import (
            DeepSeekProductInformationTranslator,
            OrderPrintImageGenerator,
            WeComOrderInfoImageGenerator,
        )

        return {
            "DeepSeekProductInformationTranslator": (
                DeepSeekProductInformationTranslator
            ),
            "OrderPrintImageGenerator": OrderPrintImageGenerator,
            "WeComOrderInfoImageGenerator": WeComOrderInfoImageGenerator,
        }[name]
    raise AttributeError(name)
