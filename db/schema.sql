-- Sample database schema for the SQL Query AI Agent (SQLite).
-- A small company: departments, employees, customers, products, orders.

PRAGMA foreign_keys = ON;

CREATE TABLE Departments (
    DepartmentID   INTEGER PRIMARY KEY,
    DepartmentName TEXT NOT NULL UNIQUE,
    Location       TEXT NOT NULL
);

CREATE TABLE Employees (
    EmployeeID   INTEGER PRIMARY KEY,
    FirstName    TEXT NOT NULL,
    LastName     TEXT NOT NULL,
    Email        TEXT NOT NULL UNIQUE,
    HireDate     DATE NOT NULL,            -- ISO format: YYYY-MM-DD
    Salary       REAL NOT NULL,
    DepartmentID INTEGER NOT NULL,
    ManagerID    INTEGER,                  -- self reference, NULL for top level
    FOREIGN KEY (DepartmentID) REFERENCES Departments(DepartmentID),
    FOREIGN KEY (ManagerID)    REFERENCES Employees(EmployeeID)
);

CREATE TABLE Customers (
    CustomerID INTEGER PRIMARY KEY,
    FirstName  TEXT NOT NULL,
    LastName   TEXT NOT NULL,
    Email      TEXT NOT NULL UNIQUE,
    City       TEXT NOT NULL,
    State      TEXT NOT NULL,              -- full state name, e.g. 'California'
    Country    TEXT NOT NULL DEFAULT 'USA',
    CreatedAt  DATE NOT NULL
);

CREATE TABLE Products (
    ProductID   INTEGER PRIMARY KEY,
    ProductName TEXT NOT NULL,
    Category    TEXT NOT NULL,
    UnitPrice   REAL NOT NULL,
    Stock       INTEGER NOT NULL
);

CREATE TABLE Orders (
    OrderID     INTEGER PRIMARY KEY,
    CustomerID  INTEGER NOT NULL,
    EmployeeID  INTEGER,                   -- sales rep who handled the order
    OrderDate   DATE NOT NULL,
    Status      TEXT NOT NULL,             -- Pending, Shipped, Delivered, Cancelled
    TotalAmount REAL NOT NULL,
    FOREIGN KEY (CustomerID) REFERENCES Customers(CustomerID),
    FOREIGN KEY (EmployeeID) REFERENCES Employees(EmployeeID)
);

CREATE TABLE OrderItems (
    OrderItemID INTEGER PRIMARY KEY,
    OrderID     INTEGER NOT NULL,
    ProductID   INTEGER NOT NULL,
    Quantity    INTEGER NOT NULL,
    UnitPrice   REAL NOT NULL,
    FOREIGN KEY (OrderID)   REFERENCES Orders(OrderID),
    FOREIGN KEY (ProductID) REFERENCES Products(ProductID)
);
