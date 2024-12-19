import sqlite3

# SQLite database file and table name
DB_FILE = "search_query_database.db"
TABLE_NAME = "New_Search_Queries"  # Replace with the table you want to delete

def delete_table(db_file, table_name):
    """
    Delete a table from the SQLite database.
    """
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    
    try:
        cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
        conn.commit()
        print(f"Table '{table_name}' has been deleted successfully.")
    except sqlite3.Error as e:
        print(f"Error occurred while deleting the table: {e}")
    finally:
        conn.close()

# Run the function
delete_table(DB_FILE, TABLE_NAME)
