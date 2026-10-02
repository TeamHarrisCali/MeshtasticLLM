"""Contract with the installed meshtastic library: the undocumented names meshllm/connection.py relies on must exist in the REAL classes.

Nothing is connected (no radio, socket, Bluetooth adapter or bleak call); this only inspects the classes. If a future meshtastic release
renames one of these names, this test fails in CI instead of the bridge silently misbehaving (see requirements.txt for the tested range)."""
import os, sys, threading
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from meshllm import connection as conn
from meshtastic.mesh_interface import MeshInterface
from meshtastic.tcp_interface import TCPInterface
from meshtastic.ble_interface import BLEInterface, BLEClient, SERVICE_UUID
from bleak import BLEDevice

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

check("the library hook _handleFromRadio exists on MeshInterface", callable(getattr(MeshInterface, "_handleFromRadio", None)))
check("BLE: everything connection.py relies on exists", conn.library_problems("ble", BLEInterface, BLEClient) == [], conn.library_problems("ble", BLEInterface, BLEClient))
check("TCP: everything connection.py relies on exists", conn.library_problems("tcp", TCPInterface) == [], conn.library_problems("tcp", TCPInterface))
check("BLEInterface still has the connect steps we override (find_device, connect) and a static scan we deliberately avoid",
      callable(BLEInterface.find_device) and callable(BLEInterface.connect) and callable(BLEInterface.scan))
check("the Meshtastic service UUID is a string", isinstance(SERVICE_UUID, str) and SERVICE_UUID)
check("a minimal BLEDevice for a bare address can be built the way connection.py builds it", conn._make_device("AA:BB:CC:DD:EE:FF").address == "AA:BB:CC:DD:EE:FF"
      and conn._make_device("AA:BB:CC:DD:EE:FF", {"path": "/p", "props": {}}).details["path"] == "/p")
check("BLEDevice is the class the library itself uses", BLEDevice is __import__("meshtastic.ble_interface", fromlist=["BLEDevice"]).BLEDevice)

# the receive hook really fires on the real TCPInterface (constructed without connecting)
Stamped = conn.stamp_rx(TCPInterface)
iface = Stamped("localhost", connectNow=False)
before = iface._rx_count
try:
    iface._handleFromRadio(b"")        # an empty FromRadio message is valid protobuf
except Exception:
    pass                               # whatever the library does with it is not our concern; the stamp comes first
check("stamp_rx counts and stamps a message handled by the real library class", iface._rx_count == before + 1 and iface._last_rx is not None, iface._rx_count)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
