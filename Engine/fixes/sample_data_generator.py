
def generate_sample_data(
    connection,
    order_count=10000,
    customer_count=1000,
    addresses_per_customer=3,
):
    """
    Creates reusable sample data in DuckDB.

    Tables:
    - customers
    - orders
    - customer_addresses
    """

    if min(order_count, customer_count, addresses_per_customer) <= 0:
        raise ValueError("All record counts must be positive.")

    # Remove old tables if they exist.
    connection.execute("DROP TABLE IF EXISTS orders")
    connection.execute("DROP TABLE IF EXISTS customer_addresses")
    connection.execute("DROP TABLE IF EXISTS customers")

    # Create customers.
    connection.execute(f"""
        CREATE TABLE customers AS
        SELECT
            customer_id,
            'Customer ' || customer_id AS customer_name
        FROM generate_series(1, {customer_count})
        AS s(customer_id)
    """)

    # Create orders.
    connection.execute(f"""
        CREATE TABLE orders AS
        SELECT
            order_id,
            ((order_id - 1) % {customer_count}) + 1 AS customer_id,
            100.0 AS amount
        FROM generate_series(1, {order_count})
        AS s(order_id)
    """)

    # Create multiple addresses for each customer.
    connection.execute(f"""
        CREATE TABLE customer_addresses AS
        SELECT
            customer_id,
            address_number,
            'City ' || address_number AS city
        FROM generate_series(1, {customer_count})
        AS c(customer_id)
        CROSS JOIN generate_series(1, {addresses_per_customer})
        AS a(address_number)
    """)

    return {
        "customers": customer_count,
        "orders": order_count,
        "customer_addresses": customer_count * addresses_per_customer,
    }
