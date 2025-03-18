import json
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timedelta

import FlowMasterClasses

# CONFIGURATION CONSTANTS
MONITOR_SERVER = True  # Flag to control monitoring server status
SERVICE_USERS = True  # Flag to control user service status
IP = socket.gethostbyname(
    socket.gethostname()
)  # Get the local machine's IP address automatically

# Ports for content servers and routing
PORTS = [8000, 8001, 8002]
ROUTING_PORT = 8080  # Port for the load balancer
MONITORING_PORT = 8081  # Port for the monitoring dashboard
SOCKET_TIMEOUT = 5  # Socket timeout in seconds

# Paths to HTML files served by different servers
FILE_PATHS = [
    "html/index1.html",  # Server on port 8000
    "html/index2.html",  # Server on port 8001
    "html/index3.html",  # Server on port 8002
    "html/tracker.html",  # Monitoring dashboard
    "html/login.html",  # Login page
    "PUP.db",  # Database of the ProjectUser Passwords
]

authenticated_sessions = {}  # Dictionary to track authenticated sessions
HEARTBEAT_INTERVAL = 2.5  # Time between heartbeat checks (in seconds)
TIMEOUT_THRESHOLD = (
    1800  # Time after which a client is considered inactive (in seconds)
)
DELAY_BETWEEN_ROUTING = 0.35  # Delay between routing requests

# SHARED STATE AND SYNCHRONIZATION
active_users = {
    port: {} for port in PORTS + [MONITORING_PORT]
}  # Track active users per port
denied_users = {}  # Track users we want to deny access
users_lock = (
    threading.Lock()
)  # Lock to protect the active_users dictionary during concurrent access
client_sockets = {}  # Dictionary to hold client sockets
connected_clients = (
    set()
)  # Set of unique client identifiers that have connected at least once
clients_lock = (
    threading.Lock()
)  # Lock to protect the connected_clients set during concurrent access

# Initialize the database and user session manager
USERNAMES = FlowMasterClasses.Database(FILE_PATHS[5])  # Allowed usernames for logins
user_session_manager = FlowMasterClasses.UserSession()  # Manage user sessions
logger = FlowMasterClasses.Logger("../server.log")  # Set up logging


def test_ports():
    """
    Test if all required ports are available before starting servers.
    Returns:
        bool: True if all ports are available, False otherwise.
    """
    for port in PORTS + [ROUTING_PORT, MONITORING_PORT]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as test_socket:
            try:
                test_socket.bind((IP, port))  # Try to bind to the port
            except socket.error:
                logger.log_error(f"Port {port} is not available!")
                return False
    return True


def signal_handler(*_):
    """
    Handle graceful shutdown on SIGINT (Ctrl+C).
    This ensures that the program exits cleanly when terminated by user.
    Args:
        *_: Ignored signal parameters.
    """
    global MONITOR_SERVER, SERVICE_USERS, client_sockets
    logger.log_info("Shutting down server - waiting for 1 second")
    MONITOR_SERVER = False
    SERVICE_USERS = False

    for _, PeerName in client_sockets.items():
        try:
            PeerName.shutdown()  # Drop the connection
        finally:
            pass

    time.sleep(1)  # Wait for a moment before exiting
    sys.exit(0)


def update_active_users():
    """
    Background task to maintain active user counts.
    Periodically checks for and removes inactive users based on
    their last activity timestamp. Runs continuously in a separate thread.
    """
    while SERVICE_USERS:
        time.sleep(HEARTBEAT_INTERVAL)  # Wait between checks
        current_time = datetime.now()

        with users_lock:  # Ensure thread-safe access to shared data
            for port in PORTS + [MONITORING_PORT]:
                # Find users who haven't sent a heartbeat within the threshold
                inactive_users = [
                    client_id
                    for client_id, last_active in active_users[port].items()
                    if (current_time - last_active)
                    > timedelta(seconds=TIMEOUT_THRESHOLD)
                ]

                # Remove inactive users
                for client_id in inactive_users:
                    del active_users[port][client_id]

            # Log current active user counts for monitoring
            logger.log_info("--- Current Active Users ---")
            for port in PORTS:
                logger.log_info(f"Port {port}: {len(active_users[port])} active users")


def get_server_loads():
    """
    Get the current load (number of active users) of each content server.
    Returns:
        dict: Dictionary mapping port numbers to user counts.
    """
    with users_lock:  # Protect shared data during read
        return {port: len(active_users[port]) for port in PORTS}


def get_monitoring_data():
    """
    Get comprehensive monitoring data for all servers.
    Formats data for the monitoring dashboard, including total counts
    and details about individual servers.
    Returns:
        dict: Dictionary with timestamp, per-server stats, and totals.
    """
    with users_lock:  # Protect shared data during read
        return {
            "timestamp": datetime.now().isoformat(),
            "servers": {
                str(port): {
                    "active_users": len(active_users[port]),
                    "users": list(active_users[port].keys()),
                }
                for port in PORTS
            },
            "total_users": sum(len(active_users[port]) for port in PORTS),
        }


def select_target_port():
    """
    Select the least loaded port for new connections (load balancing).
    Uses a simple algorithm: choose the server with the fewest active users.
    If multiple servers tie for the lowest load, selects the one with the lowest port number.
    Returns:
        int: The selected port number for the new connection.
    """
    loads = get_server_loads()
    logger.log_info(f"Current server loads: {json.dumps(loads)}")

    min_load = min(loads.values())  # Find the minimum load across all servers
    min_load_ports = [
        port for port, load in loads.items() if load == min_load
    ]  # Get all servers that have this minimum load

    # Use the lowest port number among the minimally loaded servers
    selected_port = min(min_load_ports)
    logger.log_info(f"Selected port {selected_port} with load {min_load}")
    return selected_port


def send_redirect(client_socket, port):
    """
    Send HTTP redirect response to client.
    Creates and sends a 302 Found HTTP response directing the client
    to the selected content server.
    Args:
        client_socket (socket): The client's socket connection.
        port (int): The port to redirect the client to.
    """
    redirect_response = (
        f"HTTP/1.1 302 Found\r\n" f"Location: http://{IP}:{port}/\r\n" "\r\n"
    ).encode()

    client_socket.sendall(redirect_response)
    logger.log_info(f"Sent redirect to port {port}")


def send_file(file_path, client_socket):
    """
    Send file content to client with proper HTTP headers.
    Args:
        file_path (str): Path to the file to send.
        client_socket (socket): The client's socket connection.
    """
    try:
        with open(file_path, "rb") as file:  # Read the file content
            content = file.read()

        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html\r\n"
            b"Content-Length: " + str(len(content)).encode() + b"\r\n"
            b"\r\n" + content
        )
        client_socket.sendall(response)
        logger.log_info(f"Sent file: {file_path}")

    except FileNotFoundError:
        logger.log_warning(f"File not found: {file_path}")
        client_socket.sendall(
            b"HTTP/1.1 404 Not Found\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\nFile not found."
        )
    except Exception as e:
        logger.log_error(f"Error sending file: {str(e)}")
        client_socket.sendall(
            b"HTTP/1.1 500 Internal Server Error\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\nServer error."
        )


def handle_stats_request(client_socket):
    """
    Handle requests for monitoring statistics.
    Sends JSON-formatted monitoring data to the client.
    Args:
        client_socket (socket): The client's socket connection.
    """
    stats = get_monitoring_data()  # Get current monitoring statistics

    response = (
        f"HTTP/1.1 200 OK\r\n"
        f"Content-Type: application/json\r\n"
        f"Access-Control-Allow-Origin: *\r\n"  # Allow cross-origin requests for dashboard
        f"X-Active-Users: {stats['total_users']}\r\n"  # Custom header with user count
        f"\r\n"
        f"{json.dumps(stats)}"  # Convert stats to JSON
    ).encode()

    client_socket.sendall(response)
    logger.log_info("Sent monitoring stats")


def handle_user_request(client_socket, file_path, port):
    """
    Handle incoming user HTTP requests based on the server type and request path.
    This function decodes the request, identifies the client, updates activity tracking,
    and routes the request to the appropriate handler function.
    Args:
        client_socket (socket): The client's socket connection.
        file_path (str): Path to the file to serve (if applicable).
        port (int): The port number this request was received on.
    Returns:
        bool: True if request was handled successfully, False otherwise.
    """
    try:
        if not SERVICE_USERS or not MONITOR_SERVER:
            sys.exit()

        data = client_socket.recv(9999).decode()  # Read data from client (HTTP request)
        logger.log_info(f"Received data on port {port}\n{str(data)}")

        if client_socket.fileno() == -1:  # Check if socket is still valid
            logger.log_error(f"Socket already closed on port {port}")
            return False

        client_id = None
        if "client_id=" in data:
            client_id = data.split("client_id=")[1].split(" ")[0]

        if client_id is not None and client_id in denied_users:
            # If user has been denied access, ignore him
            logger.log_info(f"Detected blocked access from {client_id} ({port})")
            msg = "Access has been denied"
            response = f"HTTP/1.1 403 Forbidden\r\nContent-Length: {len(msg)}\r\n\r\n{msg}".encode()
            client_socket.sendall(response)
            return True

        connection_type = "new"
        if client_id is not None:
            with clients_lock:  # Track if this is a new or continuing connection
                if client_id not in connected_clients:
                    connected_clients.add(client_id)
                else:
                    connection_type = "returning"

        logger.log_info(
            f"Detected '{connection_type}' connection from {client_id} on port {port}"
        )

        if client_id is not None and "/heartbeat" in data:
            with users_lock:
                active_users[port][
                    client_id
                ] = datetime.now()  # Update last active time for this client
                active_count = len(active_users[port])

            # Send minimal response with active user count in header
            msg = (
                "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                f"X-Active-Users: {active_count}\r\n"
                "Content-Length: 0\r\n\r\n"
            ).encode()

            client_socket.sendall(msg)
            return True

        if client_id is not None and "/leave" in data:  # Handle client leave requests
            with users_lock:
                if client_id in active_users[port]:
                    del active_users[port][client_id]

            msg = "{'response': 'leave received'}"
            client_socket.sendall(
                f"HTTP/1.1 200 OK\r\nContent-Length: {len(msg)}\r\n\r\n{msg}".encode()
            )
            return True

        if port == ROUTING_PORT:  # Handle routing server (load balancer) requests
            selected_port = select_target_port()
            send_redirect(client_socket, selected_port)
            return True

        # Handle content server requests
        if client_id is not None:
            with users_lock:
                active_users[port][client_id] = datetime.now()

        send_file(file_path, client_socket)

        return True

    except socket.timeout:
        logger.log_warning(f"Socket timeout occurred on port {port}")
    except Exception as e:
        logger.log_error(f"An error occurred on port {port}: {str(e)}")
    finally:
        try:  # Always ensure the socket is closed
            client_socket.close()  # Close the socket
        except Exception as e:
            logger.log_error(f"Error closing socket on port {port}: {str(e)}")
    return False


def handle_monitor_request(client_socket, file_path, port):
    """
    Handle incoming monitor HTTP requests based on the server type and request path.
    This function decodes the request, identifies the client, updates activity tracking,
    and routes the request to the appropriate handler function.
    Args:
        client_socket (socket): The client's socket connection.
        file_path (str): Path to the file to serve (if applicable).
        port (int): The port number this request was received on.
    Returns:
        bool: True if request was handled successfully, False otherwise.
    """
    try:
        if not SERVICE_USERS or not MONITOR_SERVER:
            sys.exit()

        data = client_socket.recv(9999).decode()  # Read data from client (HTTP request)
        logger.log_info(f"Received data on port {port}\n{str(data)}")

        if client_socket.fileno() == -1:  # Check if socket is still valid
            logger.log_error(f"Socket already closed on port {port}")
            return False

        # Extract request method and path
        request_line = data.split("\r\n")[0]
        method, path, _ = request_line.split(" ", 2)

        # Extract client IP for session tracking
        client_ip = client_socket.getpeername()[0]  # TO IMPLEMENT

        # Check for cookies to identify session
        session_id = None
        if "Cookie:" in data:
            cookie_line = [
                line for line in data.split("\r\n") if line.startswith("Cookie:")
            ][0]
            cookies = cookie_line.split(":", 1)[1].strip()
            cookie_parts = cookies.split(";")
            for part in cookie_parts:
                if "session_id=" in part:
                    session_id = part.split("=", 1)[1].strip()

        # Check if this is a login request
        if path == "/login" and method == "POST":
            return handle_login_request(client_socket, data)

        # Check if user is authenticated or requesting login page
        is_authenticated = user_session_manager.validate_session(session_id)

        # Root path or empty path should serve login if not authenticated
        if path == "/" or path == "":
            if is_authenticated:
                send_file(FILE_PATHS[3], client_socket)  # Serve tracker.html
            else:
                send_file(FILE_PATHS[4], client_socket)  # Serve login.html
            return True

        # Explicitly handle tracker.html request
        if path == "/tracker.html":
            if is_authenticated:
                send_file(FILE_PATHS[3], client_socket)  # Serve tracker.html
            else:
                send_redirect_to_login(client_socket)  # Redirect to log in
            return True

        # Explicitly handle login.html request
        if path == "/login.html":
            send_file(FILE_PATHS[4], client_socket)  # Always serve login page
            return True

        # Handle stats request (for authenticated users only)
        if "/stats" in path:
            if is_authenticated:
                handle_stats_request(client_socket)
            else:
                send_redirect_to_login(client_socket)
            return True

        if "/disconnect" in path:  # Handle client leave requests
            if not is_authenticated:
                send_redirect_to_login(client_socket)
                return True

            # Find the body inside the 'data'
            header_and_body = data.split("\r\n\r\n")
            user_id = None
            if len(header_and_body) > 1:
                body = header_and_body[1]

                # Parse the body - it comes as JSON
                body_json = json.loads(body)

                if "userId" in body_json:
                    user_id = body_json["userId"]

            if user_id is None:
                msg = "{'response': 'disconnect failed'}"
                client_socket.sendall(
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(msg)}\r\n\r\n{msg}".encode()
                )
                return True

            with users_lock:
                for check_port in PORTS + [MONITORING_PORT]:
                    if user_id in active_users[check_port]:
                        del active_users[check_port][user_id]

                denied_users[user_id] = True

            msg = "{'response': 'disconnect received'}"
            client_socket.sendall(
                f"HTTP/1.1 200 OK\r\nContent-Length: {len(msg)}\r\n\r\n{msg}".encode()
            )
            return True

        # For other requests, check authentication
        if not is_authenticated:
            send_redirect_to_login(client_socket)
            return True

        # Default: serve the requested file
        send_file(file_path, client_socket)
        return True

    except socket.timeout:
        logger.log_warning(f"Socket timeout occurred on port {port}")
    except Exception as e:
        logger.log_error(f"An error occurred on port {port}: {str(e)}")
    finally:
        try:  # Always ensure the socket is closed
            client_socket.close()
        except Exception as e:
            logger.log_error(f"Error closing socket on port {port}: {str(e)}")
    return False


def send_redirect_to_login(client_socket):
    """Send HTTP redirect to login page
    Args:
        client_socket (socket): The client's socket connection.
    """
    redirect_response = (
        f"HTTP/1.1 302 Found\r\n"
        f"Location: http://{IP}:{MONITORING_PORT}/login.html\r\n"
        f"\r\n"
    ).encode()

    client_socket.sendall(redirect_response)
    logger.log_info("Redirected unauthenticated user to login page")


def handle_login_request(client_socket, data):
    """Handle login POST requests
    Args:
        client_socket (socket): The client's socket connection.
        data (str): The HTTP request data.
    Returns:
        bool: True if request was handled successfully.
    """
    try:
        # Extract the request body
        body = data.split("\r\n\r\n")[1]
        login_data = json.loads(body)

        username = login_data.get("username")
        password = login_data.get("password")

        # Check credentials against USERNAMES dictionary
        if (
            username in USERNAMES.user_library
            and USERNAMES.user_library[username][0] == password
        ):
            # Generate a session ID
            session_id = user_session_manager.create_session(username)
            response = {
                "success": True,
                "message": "Login successful",
                "redirect": f"http://{IP}:{MONITORING_PORT}/tracker.html",
            }
            response_json = json.dumps(response)

            headers = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: application/json\r\n"
                f"Set-Cookie: session_id={session_id}; Path=/; HttpOnly\r\n"
                f"Content-Length: {len(response_json)}\r\n"
                f"\r\n"
            )

            client_socket.sendall((headers + response_json).encode())
            logger.log_info(f"User  {username} logged in successfully")
        else:
            # Send failure response
            response = {"success": False, "message": "Invalid username or password"}
            response_json = json.dumps(response)

            headers = (
                f"HTTP/1.1 401 Unauthorized\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(response_json)}\r\n"
                f"\r\n"
            )

            client_socket.sendall((headers + response_json).encode())
            logger.log_info(f"Failed login attempt for user {username}")

        return True
    except Exception as e:
        logger.log_error(f"Error handling login: {str(e)}")
        error_response = json.dumps({"success": False, "message": "Server error"})
        client_socket.sendall(
            f"HTTP/1.1 500 Internal Server Error\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(error_response)}\r\n"
            f"\r\n{error_response}".encode()
        )
        return True


def monitoring_server():
    """
    Start the monitoring server that provides the dashboard and stats API.
    This server runs on its own thread and handles requests for monitoring data.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind((IP, MONITORING_PORT))
        server_socket.listen()
        logger.log_info(f"Monitoring server listening on: {IP}:{MONITORING_PORT}")

        while MONITOR_SERVER:
            client_socket, _ = server_socket.accept()  # Accept incoming connections
            client_socket.settimeout(SOCKET_TIMEOUT)
            Client_PeerName = f"{client_socket.getpeername()}"
            client_sockets[Client_PeerName] = client_socket

            threading.Thread(  # Handle each request in a separate thread
                target=lambda: handle_monitor_request(
                    client_socket,
                    FILE_PATHS[4],
                    MONITORING_PORT,  # Default to login page
                )
            ).start()


def start_routing_server():
    """
    Start the main routing server (load balancer).
    This is the main entry point for clients and redirects them to the
    least loaded content server. Runs on the main thread.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as routing_socket:
        routing_socket.bind((IP, ROUTING_PORT))
        routing_socket.listen()
        logger.log_info(f"Routing server listening on: {IP}:{ROUTING_PORT}")

        last_routing_time = time.time()

        while True:
            client_socket, _ = routing_socket.accept()  # Accept incoming connections
            client_socket.settimeout(SOCKET_TIMEOUT)

            current_time = time.time()  # Implement rate limiting for routing requests
            time_since_last = current_time - last_routing_time

            # If too little time has passed since last routing, add a delay
            if time_since_last < DELAY_BETWEEN_ROUTING:
                time.sleep(DELAY_BETWEEN_ROUTING - time_since_last)

            last_routing_time = time.time()  # Update last routing time

            threading.Thread(  # Handle each routing request in a separate thread
                target=lambda: handle_user_request(client_socket, None, ROUTING_PORT)
            ).start()


def static_server(port, file_path):
    """
    Start a static content server on a specific port.
    Each static server serves one HTML file and handles client tracking.
    Args:
        port (int): Port number to listen on.
        file_path (str): Path to the HTML file to serve.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind((IP, port))
        server_socket.listen()
        logger.log_info(f"Static server listening on: {IP}:{port}")

        while MONITOR_SERVER:
            client_socket, _ = server_socket.accept()  # Accept incoming connections
            client_socket.settimeout(SOCKET_TIMEOUT)

            threading.Thread(  # Handle each request in a separate thread
                target=lambda: handle_user_request(client_socket, file_path, port)
            ).start()


def start_static_servers():
    """
    Start all static content servers in separate threads.
    Creates one server for each port/file pair defined in PORTS and FILE_PATHS.
    """
    for port, file_path in zip(PORTS, FILE_PATHS[0:3]):  # Exclude monitoring page
        threading.Thread(
            target=lambda p=port, f=file_path: static_server(p, f)
        ).start()  # Create a new thread for each static server


def main():
    """
    Main entry point for the server application.
    Tests ports, sets up signal handling, starts all servers,
    and manages the main thread.
    """
    # First check if all ports are available
    if not test_ports():
        logger.log_error("Port test failed! Please check if ports are available.")
        sys.exit(1)

    # Log access information
    logger.log_info(f"Server accessible at: http://{IP}:{ROUTING_PORT}")
    logger.log_info(f"Monitoring interface at: http://{IP}:{MONITORING_PORT}")
    logger.log_info(
        f"Direct access ports: {', '.join(f'http://{IP}:{port}' for port in PORTS)}"
    )

    # Set up signal handler for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)

    # Start background task for updating active users
    threading.Thread(target=update_active_users, daemon=True).start()

    # Start all content servers in separate threads
    start_static_servers()

    # Start monitoring server in a separate thread
    threading.Thread(target=monitoring_server, daemon=True).start()

    # Start routing server on the main thread
    start_routing_server()


# Entry point when script is run directly
if __name__ == "__main__":
    main()
