"""Test-only fixture. Run inside a container spawned by
dockerized_stdio_args() to check, from inside the container, whether
network access and filesystem writes are actually blocked -- not just
that the docker run command was constructed with the right flags."""
import json
import socket

result = {"network_blocked": None, "fs_write_blocked": None}

try:
    s = socket.create_connection(("1.1.1.1", 53), timeout=2)
    s.close()
    result["network_blocked"] = False
except OSError:
    result["network_blocked"] = True

try:
    with open("/should-not-be-writable", "w") as f:
        f.write("x")
    result["fs_write_blocked"] = False
except OSError:
    result["fs_write_blocked"] = True

print(json.dumps(result))
