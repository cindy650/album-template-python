from backend.config import settings
from backend.database import connection_scope, table_columns


def main() -> None:
    with connection_scope(settings.mysql_url) as connection:
        columns = {row["name"] for row in table_columns(connection, "orders")}
        if "wecom_preview_sent" not in columns:
            connection.execute(
                "ALTER TABLE orders "
                "ADD COLUMN wecom_preview_sent INTEGER NOT NULL DEFAULT 0"
            )
            print("[数据库迁移] orders.wecom_preview_sent 添加成功", flush=True)
            # Before this migration, status 0 could only advance after the
            # preview images had been sent successfully. Preserve that known
            # history while decoupling future status changes from delivery.
            connection.execute(
                "UPDATE orders SET wecom_preview_sent = 1 WHERE status >= 1"
            )
            print("[数据库迁移] 历史已推进订单的企业微信发送记录回填完成", flush=True)
        else:
            print("[数据库迁移] orders.wecom_preview_sent 已存在，跳过", flush=True)


if __name__ == "__main__":
    main()
