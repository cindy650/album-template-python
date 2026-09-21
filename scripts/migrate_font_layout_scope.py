from backend.config import settings
from backend.database import connection_scope, table_columns


def main() -> None:
    with connection_scope(settings.mysql_url) as connection:
        columns = {row["name"] for row in table_columns(connection, "font_layout_library")}
        if "layout_scope" not in columns:
            connection.execute(
                "ALTER TABLE font_layout_library "
                "ADD COLUMN layout_scope VARCHAR(20) NOT NULL DEFAULT 'size'"
            )
        connection.execute("""CREATE TABLE IF NOT EXISTS inner_page_font_layout_library (id INTEGER PRIMARY KEY AUTO_INCREMENT, shop_id INTEGER NOT NULL, product_id INTEGER NULL, name VARCHAR(255) NOT NULL, sort_key VARCHAR(255) NOT NULL DEFAULT '', preview_image_path TEXT NOT NULL, layers_json LONGTEXT NOT NULL, created_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL)""")
        connection.execute("INSERT IGNORE INTO inner_page_font_layout_library (id,shop_id,product_id,name,sort_key,preview_image_path,layers_json,created_at,updated_at) SELECT id,shop_id,product_id,name,sort_key,preview_image_path,layers_json,created_at,updated_at FROM font_layout_library WHERE layout_scope='inner_page'")
        connection.execute("DELETE FROM font_layout_library WHERE layout_scope='inner_page'")


if __name__ == "__main__":
    main()
