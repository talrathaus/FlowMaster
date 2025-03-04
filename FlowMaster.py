#!env/python3
# Short explanation of everything up until 03/03/2025, please remember to update this Tal...


"""
Overview: This is a multithreaded server that handles multiple client requests at once. It serves
HTML files from different ports and directs clients to the right server based on current load. It
also keeps track of active users and removes inactive ones. Additionally, it introduces a monitoring
server to track real-time user activity and server load, and implements a delay mechanism for
routing to ensure balanced load distribution.

Technical Architecture:
- Uses TCP sockets for reliable client-server communication
- Implements thread-based concurrency for handling multiple simultaneous connections
- Maintains shared state with thread-safe mechanisms
- Provides load balancing through a routing server architecture
- Implements heartbeat mechanism for user activity tracking
- Introduces a monitoring server for real-time statistics
- Implements a delay mechanism in the routing server to ensure even distribution

What it does:
1. Port Check: Ensures all required ports are available before startup.
2. Static Servers: Serves HTML files from different ports.
3. Routing Server: A main server (port ROUTING_PORT) that sends clients to the least busy server with a delay mechanism.
4. Monitoring Server: Provides real-time data about server load and active users.
5. User Tracking: Regularly updates the list of active users and removes inactive ones.
6. Graceful Shutdown: When stopped, it logs active users before exiting.

Main Components:
1. Socket (socket module):
   - Used for network communication
   - Implements TCP protocol for reliable data transfer
   - Handles client connections and data transmission

2. Threading (threading module):
   - Enables concurrent handling of multiple clients
   - Provides thread-safe mechanisms (Lock) for shared resource access
   - Runs background tasks for user tracking

3. Signal (signal module):
   - Handles system signals for graceful shutdown
   - Ensures proper cleanup on program termination

4. Time/Datetime:
   - Manages user session timeouts
   - Tracks user activity timestamps
   - Handles periodic maintenance tasks
   - Introduces delay in routing to optimize load distribution

5. Logging:
   - Records server activities and errors
   - Provides debugging information
   - Tracks user connections and server state

Why TCP instead of UDP?
- TCP Benefits:
  * Guaranteed delivery of data
  * Automatic packet ordering
  * Flow control and congestion control
  * Connection-oriented communication
  * Error checking and recovery
- UDP would be unsuitable because:
  * No guarantee of packet delivery
  * No packet ordering
  * No connection state tracking
  * No flow control

Key Functions Explained:
test_ports():
- Purpose: Validates port availability before server startup
- Process: Attempts to bind to each required port
- Error handling: Logs and returns False if any port is unavailable
- Importance: Prevents startup failures due to port conflicts

signal_handler():
- Purpose: Handles system interrupts (Ctrl+C)
- Process: Initiates graceful shutdown sequence
- Importance: Ensures proper cleanup and resource release

update_active_users():
- Purpose: Maintains accurate user activity tracking
- Process:
  * Runs periodically (HEARTBEAT_INTERVAL)
  * Checks last activity timestamp for each user
  * Removes users inactive beyond TIMEOUT_THRESHOLD
- Thread safety: Uses users_lock for safe state updates

get_server_loads():
- Purpose: Tracks current load on each server
- Process: Counts active users per port
- Thread safety: Uses users_lock for consistent readings

get_monitoring_data():
- Purpose: Provides real-time statistics of server load
- Process: Compiles data from active_users dictionary
- Output: JSON response containing load statistics
- Thread safety: Uses users_lock for accurate readings

select_target_port():
- Purpose: Implements load balancing logic with delay mechanism
- Process:
  * Gets current server loads
  * Identifies servers with minimum load
  * Selects lowest port number among the least loaded servers
  * Ensures delay mechanism is respected for fairness
- Optimization: Favors lower port numbers for consistent distribution

send_redirect():
- Purpose: Implements HTTP redirection
- Process: Sends 302 Found response with new location
- Format: Follows HTTP/1.1 specification for redirects

send_file():
- Purpose: Serves static HTML content
- Process:
  * Reads file content
  * Generates HTTP response headers
  * Sends complete response to client
- Error handling: Handles file not found and server errors

handle_request():
- Purpose: Central request processing logic
- Process:
  * Parses incoming HTTP requests
  * Identifies clients using ID or IP
  * Handles specialized requests (heartbeat, leave, monitoring stats)
  * Routes or serves content based on port
- Thread safety: Uses multiple locks for shared state access

start_routing_server():
- Purpose: Implements main load balancer with delay mechanism
- Process:
  * Accepts incoming connections
  * Creates new thread for each client
  * Delegates to handle_request()
  * Implements routing delay for load balancing
- Configuration: Uses ROUTING_PORT

static_server():
- Purpose: Serves static content
- Process:
  * Binds to specified port
  * Accepts connections
  * Serves associated HTML file
- Thread safety: Creates new thread per client

start_static_servers():
- Purpose: Initializes content servers
- Process: Creates thread for each port/file combination
- Configuration: Uses PORTS and FILE_PATHS lists

main():
- Purpose: Application entry point
- Process:
  * Validates ports
  * Sets up signal handling
  * Starts user tracking
  * Initializes all servers
- Error handling: Exits on port test failure

Configuration Constants Explained:
- IP: Server's IP address (automatically detected)
- PORTS: Available ports for static content (8000, 8001, 8002)
- ROUTING_PORT: Load balancer port (8080)
- MONITORING_PORT: Monitoring server port
- SOCKET_TIMEOUT: Client connection timeout (5 seconds)
- FILE_PATHS: Locations of HTML files to serve
- HEARTBEAT_INTERVAL: User activity check frequency (2.5 seconds)
- TIMEOUT_THRESHOLD: User inactivity limit (10 seconds)
- DELAY_BETWEEN_ROUTING: Delay applied before redirecting clients to ensure balanced distribution (0.005 seconds)

Shared State Management:
- active_users: Dictionary tracking user activity per port
- users_lock: Thread lock for active_users access
- connected_clients: Set of all client identifiers
- clients_lock: Thread lock for connected_clients access

Logging Configuration:
- Logs to both file (server.log) and console
- Includes timestamps and log levels
- Captures important events and errors
"""

import socket
import threading
import signal
import sys
import time
from datetime import datetime, timedelta
import logging
import json

# CONFIGURATION CONSTANTS
MONITOR_SERVER = True
SERVICE_USERS = True

IP = socket.gethostbyname(
    socket.gethostname()
)  # Get the local machine's IP address automatically
PORTS = [
    8000,
    8001,
    8002,
]  # Ports for content servers that will host the actual web pages
ROUTING_PORT = (
    8080  # Port for the load balancer that will redirect users to the least busy server
)
MONITORING_PORT = 8081  # Port for the monitoring dashboard to view server statistics
SOCKET_TIMEOUT = 5  # Socket timeout in seconds to prevent hanging connections
FILE_PATHS = [  # Paths to HTML files served by different servers
    "html/index1.html",  # Server on port 8000
    "html/index2.html",  # Server on port 8001
    "html/index3.html",  # Server on port 8002
    "html/tracker.html",  # Monitoring dashboard
]

HEARTBEAT_INTERVAL = (
    2.5  # Time between heartbeat checks to verify client connections (in seconds)
)
TIMEOUT_THRESHOLD = 10  # Time after which a client is considered inactive (in seconds)

# A small delay between routing requests to prevent overwhelming servers (in seconds)
DELAY_BETWEEN_ROUTING = 0.35

# SHARED STATE AND SYNCHRONIZATION

active_users = {
    port: {} for port in PORTS + [MONITORING_PORT]
}  # Dictionary tracking active users per port, with format: {port: {client_id: last_active_time}}
denied_users = (
    {}
)  # Dictionary tracking users we want to deny access (i.e. disconnect them), with format: {client_id}

users_lock = (
    threading.Lock()
)  # Lock to protect the active_users dictionary during concurrent access

client_sockets = {}

connected_clients = (
    set()
)  # Set of unique client identifiers that have connected at least once

clients_lock = (
    threading.Lock()
)  # Lock to protect the connected_clients set during concurrent access

# LOGGING CONFIGURATION

logging.basicConfig(  # Set up logging to both file and console for easier debugging
    level=logging.INFO,  # Set minimum log level to INFO
    format="%(asctime)s - %(levelname)s - %(message)s",  # Log format with timestamp
    handlers=[
        logging.FileHandler("../server.log"),  # Save logs to file
        logging.StreamHandler(sys.stdout),  # Display logs in console
    ],
)


def test_ports():
    """
    Test if all required ports are available before starting servers.
    Returns:
        bool: True if all ports are available, False otherwise.
    """
    for port in PORTS + [ROUTING_PORT, MONITORING_PORT]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as test_socket:
            try:
                test_socket.bind((IP, port))
                # If binding succeeds, the port is available
            except socket.error:
                logging.error("Port %d is not available!", port)
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

    logging.info("Shutting down server - waiting for 1 second")

    MONITOR_SERVER = False
    SERVICE_USERS = False

    for _, peername in client_sockets.items():
        try:
            # Drop the connection so that we can exit gracefully
            peername.shutdown()
        except:
            # We don't care about failed shutdown
            pass

    time.sleep(1)

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
            # Check all servers including monitoring
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
            logging.info("--- Current Active Users ---")
            for port in PORTS:
                logging.info("Port %d: %d active users", port, len(active_users[port]))


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
    logging.info("Current server loads: %s", json.dumps(loads))

    min_load = min(loads.values())  # Find the minimum load across all servers

    min_load_ports = [
        port for port, load in loads.items() if load == min_load
    ]  # Get all servers that have this minimum load

    # Use the lowest port number among the minimally loaded servers
    # This provides consistent behavior when multiple servers have the same load
    selected_port = min(min_load_ports)

    logging.info("Selected port %d with load %d", selected_port, min_load)
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
    redirect_response = (  # Format standard HTTP redirect response
        f"HTTP/1.1 302 Found\r\n" f"Location: http://{IP}:{port}/\r\n" "\r\n"
    ).encode()

    client_socket.sendall(redirect_response)
    logging.info("Sent redirect to port %d", port)


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

        response = (  # Construct HTTP response with headers and content
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html\r\n"
            b"Content-Length: " + str(len(content)).encode() + b"\r\n"
            b"\r\n" + content
        )
        client_socket.sendall(response)
        logging.info("Sent file: %s", file_path)

    except FileNotFoundError:
        logging.warning(
            "File not found: %s", file_path
        )  # Handle case where file doesn't exist
        client_socket.sendall(
            b"HTTP/1.1 404 Not Found\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\nFile not found."
        )
    except Exception as e:
        logging.error("Error sending file: %s", str(e))  # Handle any other errors
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

    response = (  # Format HTTP response with JSON content
        f"HTTP/1.1 200 OK\r\n"
        f"Content-Type: application/json\r\n"
        f"Access-Control-Allow-Origin: *\r\n"  # Allow cross-origin requests for dashboard
        f"X-Active-Users: {stats['total_users']}\r\n"  # Custom header with user count
        f"\r\n"
        f"{json.dumps(stats)}"  # Convert stats to JSON
    ).encode()

    client_socket.sendall(response)
    logging.info("Sent monitoring stats")


def handle_request(client_socket, file_path, port):
    """
    Handle incoming HTTP requests based on the server type and request path.
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
            # Stop the thread
            sys.exit()

        data = client_socket.recv(9999).decode()  # Read data from client (HTTP request)
        logging.debug("Received data on port %d\n%s", port, str(data))

        if client_socket.fileno() == -1:  # Check if socket is still valid
            logging.error("Socket already closed on port %d", port)
            return False

        client_address = client_socket.getpeername()[
            0
        ]  # Identify the client - either by client_id parameter or IP address

        client_id = None
        if "client_id=" in data:
            client_id = data.split("client_id=")[1].split(" ")[0]

        identifier = client_id if client_id else client_address

        if identifier in denied_users:
            # If user has been denied access, ignore him
            logging.info("^ Detected blocked access from %s ^", identifier)
            msg = "Access has been denied"
            response = (
                f"HTTP/1.1 403 Forbidden\r\nContent-Length: %d\r\n\r\n{msg}".encode()
            )
            client_socket.sendall(response)
            return True

        with clients_lock:  # Track if this is a new or continuing connection
            if identifier not in connected_clients:
                connection_type = "new"
                connected_clients.add(identifier)
            else:
                connection_type = "continuing"

        logging.info("^ Detected %s connection from %s ^", connection_type, identifier)

        if port == MONITORING_PORT:  # Handle monitoring server requests
            if "/stats" in data:
                handle_stats_request(client_socket)
                return True

            if "/disconnect" in data:  # Handle client leave requests
                # Find the body inside the 'data'
                header_and_body = data.split("\r\n\r\n")

                user_id = None
                if len(header_and_body) > 1:
                    body = header_and_body[1]

                    # Parse the body - it comes as json
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

            send_file(file_path, client_socket)
            return True

        if "/heartbeat" in data:  # Handle heartbeat requests (keep-alive signals)
            with users_lock:
                active_users[port][
                    identifier
                ] = datetime.now()  # Update last active time for this client
                active_count = len(active_users[port])

            headers = [  # Send minimal response with active user count in header
                b"HTTP/1.1 200 OK",
                b"Content-Type: text/plain",
                f"X-Active-Users: {active_count}".encode(),
                b"Content-Length: 0",
                b"",
                b"",
            ]
            client_socket.sendall(b"\r\n".join(headers))
            return True

        if "/leave" in data:  # Handle client leave requests
            with users_lock:
                if identifier in active_users[port]:
                    del active_users[port][identifier]

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
        with users_lock:
            active_users[port][identifier] = datetime.now()

        send_file(file_path, client_socket)

        return True

    except socket.timeout:
        logging.warning(
            "Socket timeout occurred on port %d", port
        )  # Handle timeout error
    except Exception as e:
        logging.error(
            "An error occurred on port %d: %s", port, str(e)
        )  # Handle any other errors
    finally:
        try:  # Always ensure the socket is closed
            # noinspection PyArgumentList
            client_socket.close()  # the comment above is to stop pep8 from being mad
        except Exception as e:
            logging.error("Error closing socket on port %d: %s", port, str(e))
    return False


def monitoring_server():
    """
    Start the monitoring server that provides the dashboard and stats API.
    This server runs on its own thread and handles requests for monitoring data.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind((IP, MONITORING_PORT))
        server_socket.listen()
        logging.info("Monitoring server listening on: %s:%d", IP, MONITORING_PORT)

        while MONITOR_SERVER:
            (
                client_socket,
                _,
            ) = server_socket.accept()  # Accept incoming connections

            client_socket.settimeout(SOCKET_TIMEOUT)
            client_peername = f"{client_socket.getpeername()}"
            client_sockets[client_peername] = client_socket

            threading.Thread(  # Handle each request in a separate thread
                target=lambda: handle_request(
                    client_socket, FILE_PATHS[3], MONITORING_PORT
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
        logging.info("Routing server listening on: %s:%d", IP, ROUTING_PORT)

        last_routing_time = time.time()

        while True:
            (
                client_socket,
                _,
            ) = routing_socket.accept()  # Accept incoming connections
            client_socket.settimeout(SOCKET_TIMEOUT)

            current_time = time.time()  # Implement rate limiting for routing requests
            time_since_last = current_time - last_routing_time

            # If too little time has passed since last routing, add a delay
            # This prevents overwhelming the system with too many redirects at once
            if time_since_last < DELAY_BETWEEN_ROUTING:
                time.sleep(DELAY_BETWEEN_ROUTING - time_since_last)

            last_routing_time = time.time()  # Update last routing time

            threading.Thread(  # Handle each routing request in a separate thread
                target=lambda: handle_request(client_socket, None, ROUTING_PORT)
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
        logging.info("Static server listening on: %s:%d", IP, port)

        while MONITOR_SERVER:
            (
                client_socket,
                _,
            ) = server_socket.accept()  # Accept incoming connections
            client_socket.settimeout(SOCKET_TIMEOUT)

            threading.Thread(  # Handle each request in a separate thread
                target=lambda: handle_request(client_socket, file_path, port)
            ).start()


def start_static_servers():
    """
    Start all static content servers in separate threads.
    Creates one server for each port/file pair defined in PORTS and FILE_PATHS.
    """
    for port, file_path in zip(PORTS, FILE_PATHS[:-1]):  # Exclude monitoring page
        threading.Thread(
            target=lambda p=port, f=file_path: static_server(p, f)
        ).start()  # Create a new thread for each static server
        # Note: p=port, f=file_path creates a closure to capture current values


def main():
    """
    Main entry point for the server application.
    Tests ports, sets up signal handling, starts all servers,
    and manages the main thread.
    """
    # First check if all ports are available
    if not test_ports():
        logging.error("Port test failed! Please check if ports are available.")
        sys.exit(1)

    # Log access information
    logging.info("Server accessible at: http://%s:%d", IP, ROUTING_PORT)
    logging.info("Monitoring interface at: http://%s:%d", IP, MONITORING_PORT)
    logging.info(
        "Direct access ports: %s", ", ".join(f"http://{IP}:{port}" for port in PORTS)
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
    # This is the last call and will block until program termination
    start_routing_server()


# Entry point when script is run directly
if __name__ == "__main__":
    main()
