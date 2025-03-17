import sqlite3

class Database:
    def __init__(self, db_file_path):  # Fixed method name and added self parameter
        self.user_library = {}
        try:
            # Connect to the SQLite database
            conn = sqlite3.connect(db_file_path)
            cursor = conn.cursor()

            # Query all user records from the UserPassPerm table
            cursor.execute("SELECT Username, Password, Perm FROM UserPassPerm")
            rows = cursor.fetchall()

            # Process each row and add to the dictionary
            for row in rows:
                username = row[0]
                password = row[1]
                permission = row[2]

                # Add to the dictionary with the required format
                self.user_library[username] = [password, permission]

            # Close the connection
            conn.close()

        except sqlite3.Error as e:
            print(f"SQLite error: {e}")
            # Don't return a value in __init__
        except Exception as e:
            print(f"Error: {e}")
            # Don't return a value in __init__
