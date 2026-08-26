ORDER_STATUS_SEED = (
    (0, "新订单", "发送示意图"),
    (1, "示意图已发送/客户确认中", "客户已确认"),
    (2, "待生产", "确认生产"),
    (3, "生产中", "完成生产"),
    (4, "待发货", "已发货"),
    (5, "订单已完成", ""),
)

DEFAULT_ORDER_STATUS = ORDER_STATUS_SEED[0][0]
PREVIEW_SENT_ORDER_STATUS = 1
PRODUCTION_ORDER_STATUS = 3
