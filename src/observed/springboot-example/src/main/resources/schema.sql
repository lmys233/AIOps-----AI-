CREATE TABLE IF NOT EXISTS java_catalog_items (id BIGINT PRIMARY KEY, name VARCHAR(80) NOT NULL, price DECIMAL(12,2) NOT NULL);
INSERT INTO java_catalog_items (id,name,price) SELECT 1,'示例商品',12.50 WHERE NOT EXISTS (SELECT 1 FROM java_catalog_items WHERE id=1);
