#!env/python3

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

# IP = socket.gethostbyname(
#     socket.gethostname()
# )  # Get the local machine's IP address automatically
IP = socket.gethostbyname(socket.gethostname())

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
    # "html/login.html"
]

USERNAMES = {"Tal" : "test",}

HEARTBEAT_INTERVAL = (
    2.5  # Time between heartbeat checks to verify client connections (in seconds)
)
TIMEOUT_THRESHOLD = (
    1800  # Time after which a client is considered inactive (in seconds)
)

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
            # Stop the thread
            sys.exit()

        data = client_socket.recv(9999).decode()  # Read data from client (HTTP request)
        logging.debug("Received data on port %d\n%s", port, str(data))

        if client_socket.fileno() == -1:  # Check if socket is still valid
            logging.error("Socket already closed on port %d", port)
            return False

        client_id = None
        if "client_id=" in data:
            client_id = data.split("client_id=")[1].split(" ")[0]

        if client_id is not None and client_id in denied_users:
            # If user has been denied access, ignore him
            logging.info("^ Detected blocked access from %s (%d)^", client_id, port)
            msg = "Access has been denied"
            response = (
                f"HTTP/1.1 403 Forbidden\r\nContent-Length: %d\r\n\r\n{msg}".encode()
            )
            client_socket.sendall(response)
            return True

        connection_type = "new"
        if client_id is not None:
            with clients_lock:  # Track if this is a new or continuing connection
                if client_id not in connected_clients:
                    connected_clients.add(client_id)
                else:
                    connection_type = "returning"

        logging.info(
            "^ Detected '%s' connection from %s on port %d ^",
            connection_type,
            client_id,
            port,
        )

        if (
            client_id is not None and "/heartbeat" in data
        ):  # Handle heartbeat requests (keep-alive signals)
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


def handle_monitor_request(client_socket, file_path, port):
    """Handle incoming monitor HTTP requests based on the server type and request path.
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
                target=lambda: handle_monitor_request(
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
        logging.info("Static server listening on: %s:%d", IP, port)

        while MONITOR_SERVER:
            (
                client_socket,
                _,
            ) = server_socket.accept()  # Accept incoming connections

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
