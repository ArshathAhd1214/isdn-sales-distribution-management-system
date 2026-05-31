USE ISDN_DB;
GO

-- =========================================
-- 1. SAFE CHECK: USERS TABLE
-- =========================================
IF OBJECT_ID('dbo.Users', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Users
    (
        user_id INT IDENTITY(1,1) PRIMARY KEY,
        full_name NVARCHAR(100) NOT NULL,
        email NVARCHAR(150) NOT NULL UNIQUE,
        password_hash NVARCHAR(255) NOT NULL,
        role NVARCHAR(20) NOT NULL,
        is_active BIT NOT NULL DEFAULT 1,
        created_at DATETIME NOT NULL DEFAULT GETDATE(),

        CONSTRAINT CHK_Users_Role
        CHECK (role IN ('customer', 'staff', 'admin'))
    );
END
GO

-- =========================================
-- 2. SAFE CHECK: PRODUCTS TABLE
-- =========================================
IF OBJECT_ID('dbo.Products', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Products
    (
        product_id INT IDENTITY(1,1) PRIMARY KEY,
        product_name NVARCHAR(150) NOT NULL,
        category NVARCHAR(100) NOT NULL,
        price DECIMAL(10,2) NOT NULL,
        stock_quantity INT NOT NULL DEFAULT 0,
        is_active BIT NOT NULL DEFAULT 1,
        created_at DATETIME NOT NULL DEFAULT GETDATE()
    );
END
GO

-- =========================================
-- 3. SAFE CHECK: ORDERS TABLE
-- =========================================
IF OBJECT_ID('dbo.Orders', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Orders
    (
        order_id INT IDENTITY(1,1) PRIMARY KEY,
        customer_id INT NOT NULL,
        order_date DATETIME NOT NULL DEFAULT GETDATE(),
        total_amount DECIMAL(10,2) NOT NULL DEFAULT 0,
        order_status NVARCHAR(30) NOT NULL DEFAULT 'Pending',

        CONSTRAINT FK_Orders_Users
        FOREIGN KEY (customer_id) REFERENCES dbo.Users(user_id),

        CONSTRAINT CHK_Orders_Status
        CHECK (order_status IN ('Pending', 'Confirmed', 'Packed', 'Out for Delivery', 'Delivered', 'Cancelled'))
    );
END
GO

-- =========================================
-- 4. SAFE CHECK: ORDERITEMS TABLE
-- =========================================
IF OBJECT_ID('dbo.OrderItems', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.OrderItems
    (
        order_item_id INT IDENTITY(1,1) PRIMARY KEY,
        order_id INT NOT NULL,
        product_id INT NOT NULL,
        quantity INT NOT NULL,
        unit_price DECIMAL(10,2) NOT NULL,
        subtotal AS (quantity * unit_price) PERSISTED,

        CONSTRAINT FK_OrderItems_Orders
        FOREIGN KEY (order_id) REFERENCES dbo.Orders(order_id),

        CONSTRAINT FK_OrderItems_Products
        FOREIGN KEY (product_id) REFERENCES dbo.Products(product_id)
    );
END
GO

-- =========================================
-- 5. SAFE CHECK: DELIVERIES TABLE
-- =========================================
IF OBJECT_ID('dbo.Deliveries', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Deliveries
    (
        delivery_id INT IDENTITY(1,1) PRIMARY KEY,
        order_id INT NOT NULL UNIQUE,
        delivery_status NVARCHAR(30) NOT NULL DEFAULT 'Pending',
        estimated_date DATE NULL,
        delivered_date DATE NULL,
        tracking_note NVARCHAR(255) NULL,

        CONSTRAINT FK_Deliveries_Orders
        FOREIGN KEY (order_id) REFERENCES dbo.Orders(order_id),

        CONSTRAINT CHK_Deliveries_Status
        CHECK (delivery_status IN ('Pending', 'Processing', 'Packed', 'Out for Delivery', 'Delivered'))
    );
END
GO

-- =========================================
-- 6. SAFE CHECK: EMAIL INDEX
-- =========================================
IF NOT EXISTS
(
    SELECT 1
    FROM sys.indexes
    WHERE name = 'IX_Users_Email'
      AND object_id = OBJECT_ID('dbo.Users')
)
BEGIN
    CREATE INDEX IX_Users_Email ON dbo.Users(email);
END
GO

-- =========================================
-- 7. INSERT SAMPLE USERS ONLY IF MISSING
-- =========================================
IF NOT EXISTS (SELECT 1 FROM dbo.Users WHERE email = 'customer@test.com')
BEGIN
    INSERT INTO dbo.Users (full_name, email, password_hash, role, is_active)
    VALUES ('Customer User', 'customer@test.com', '123456', 'customer', 1);
END
GO

IF NOT EXISTS (SELECT 1 FROM dbo.Users WHERE email = 'staff@test.com')
BEGIN
    INSERT INTO dbo.Users (full_name, email, password_hash, role, is_active)
    VALUES ('Staff User', 'staff@test.com', '123456', 'staff', 1);
END
GO

IF NOT EXISTS (SELECT 1 FROM dbo.Users WHERE email = 'admin@test.com')
BEGIN
    INSERT INTO dbo.Users (full_name, email, password_hash, role, is_active)
    VALUES ('Admin User', 'admin@test.com', '123456', 'admin', 1);
END
GO

-- =========================================
-- 8. INSERT SAMPLE PRODUCTS ONLY IF MISSING
-- =========================================
IF NOT EXISTS (SELECT 1 FROM dbo.Products WHERE product_name = 'Milk Powder 1kg')
BEGIN
    INSERT INTO dbo.Products (product_name, category, price, stock_quantity, is_active)
    VALUES
    ('Milk Powder 1kg', 'Beverages', 1200.00, 50, 1),
    ('Bath Soap', 'Personal Care', 150.00, 200, 1),
    ('Soft Drink 1L', 'Beverages', 220.00, 100, 1),
    ('Dish Wash Liquid', 'Home Cleaning', 480.00, 75, 1),
    ('Instant Noodles Pack', 'Packaged Foods', 130.00, 150, 1);
END
GO

-- =========================================
-- 9. VIEW DATA
-- =========================================
SELECT * FROM dbo.Users;
SELECT * FROM dbo.Products;
GO