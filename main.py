import usb.core
import usb.util
import threading
import time
import json
import paho.mqtt.client as mqtt

# LEGO ToyPad vendor and product IDs
VENDOR_ID = 0x0e6f
PRODUCT_ID = 0x0241

# Initialization command
TOYPAD_INIT = [
    0x55, 0x0f, 0xb0, 0x01, 0x28, 0x63, 0x29, 0x20, 0x4c, 0x45, 0x47, 0x4f, 0x20, 0x32, 0x30, 0x31, 0x34,
    0xf7, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
]

# MQTT Configuration
MQTT_BROKER = "simmarith.com"
MQTT_PORT = 1883
MQTT_TOPIC_TAG_ADDED = "toypad/tagAdded"
MQTT_TOPIC_TAG_REMOVED = "toypad/tagRemoved"
MQTT_TOPIC_TAG_CHANGED = "toypad/tagChanged"

# Message constants
MSG_NORMAL = 0x55
TAG_ADDED = 0
BGCOLOR = [12, 12, 12]

# Helper functions
def calculate_checksum(command):
    return sum(command) % 256

def send_command(dev, command):
    checksum = calculate_checksum(command)
    message = command + [checksum] + [0x00] * (32 - len(command) - 1)  # Pad to 32 bytes
    endpoint_out = dev[0][(0, 0)][1]
    endpoint_out.write(message)

# ToyPad class with threading, callbacks, and automatic reconnection
class Toypad:
    def __init__(self, vendor_id=VENDOR_ID, product_id=PRODUCT_ID):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.device = None
        self.detected_tags = {}
        self.tagNew = None
        self.tagGone = None
        self.tagChange = None
        self.listening = False
        self.listener_thread = None
        self.connect()

    def connect(self):
        # Attempt to connect to the ToyPad
        self.device = usb.core.find(idVendor=self.vendor_id, idProduct=self.product_id)
        if self.device:
            try:
                if self.device.is_kernel_driver_active(0):
                    self.device.detach_kernel_driver(0)
                self.device.set_configuration()
                self.init()
                self.listening = True
                self.listener_thread = threading.Thread(target=self.listen_for_tags)
                self.listener_thread.start()
                print("ToyPad connected and listening.")
                self.set_pad_color(0, BGCOLOR)
                self.set_pad_color(1, BGCOLOR)
                self.set_pad_color(2, BGCOLOR)
            except usb.core.USBError as e:
                print("Failed to initialize ToyPad:", e)
                self.device = None

    def init(self):
        send_command(self.device, TOYPAD_INIT)

    def listen_for_tags(self):
        endpoint_in = self.device[0][(0, 0)][0]
        while self.listening:
            try:
                bytelist = endpoint_in.read(32, timeout=1000)
                if not bytelist or bytelist[0] != 0x56:
                    continue

                pad_num = bytelist[2]
                uid_bytes = bytes(bytelist[6:13])
                action = bytelist[5]
                uid = uid_bytes.hex()

                if action == TAG_ADDED:
                    if uid not in self.detected_tags:
                        self.detected_tags[uid] = pad_num
                        if self.tagNew:
                            self.tagNew(uid, pad_num)
                    elif self.detected_tags[uid] != pad_num:
                        old_pad = self.detected_tags[uid]
                        self.detected_tags[uid] = pad_num
                        if self.tagChange:
                            self.tagChange(uid, old_pad, pad_num)
                else:
                    if uid in self.detected_tags:
                        old_pad = self.detected_tags.pop(uid)
                        if self.tagGone:
                            self.tagGone(uid, old_pad)
            except usb.core.USBError as e:
                if e.errno == 19:  # USB device disconnected
                    print("ToyPad disconnected.")
                    self.handle_disconnection()
                    break
                elif e.errno != 110:
                    print("USB Error:", e)

    def handle_disconnection(self):
        self.listening = False
        self.device = None
        self.listener_thread = None  # Set to None to allow for restarting
        self.reconnect()

    def reconnect(self):
        print("Attempting to reconnect to ToyPad...")
        while not self.device:
            time.sleep(2)
            self.connect()

    def set_pad_color(self, pad, color):
        command = [MSG_NORMAL, 0x06, 0xc0, 0x02, pad] + color
        send_command(self.device, command)

    def set_tag_callbacks(self, tag_new=None, tag_gone=None, tag_change=None):
        self.tagNew = tag_new
        self.tagGone = tag_gone
        self.tagChange = tag_change

    def close(self):
        self.listening = False
        if self.listener_thread:
            self.listener_thread.join()
        if self.device:
            usb.util.dispose_resources(self.device)

# Initialize MQTT client
client = mqtt.Client()
client.connect(MQTT_BROKER, MQTT_PORT, 60)
client.loop_start()

# Callback functions with JSON-formatted MQTT messages
def tag_new(uid, pad):
    toypad.set_pad_color(pad, [255, 0, 0])
    print(f"New tag detected: UID={uid} on Pad {pad}")
    payload = json.dumps({"event": "tag_added", "uid": uid, "pad": pad})
    client.publish(MQTT_TOPIC_TAG_ADDED, payload)

def tag_gone(uid, pad):
    toypad.set_pad_color(pad, BGCOLOR)
    print(f"Tag removed: UID={uid} from Pad {pad}")
    payload = json.dumps({"event": "tag_removed", "uid": uid, "pad": pad})
    client.publish(MQTT_TOPIC_TAG_REMOVED, payload)

def tag_change(uid, old_pad, new_pad):
    print(f"Tag moved: UID={uid} from Pad {old_pad} to Pad {new_pad}")
    payload = json.dumps({"event": "tag_changed", "uid": uid, "old_pad": old_pad, "new_pad": new_pad})
    client.publish(MQTT_TOPIC_TAG_CHANGED, payload)

# Main script
if __name__ == "__main__":
    toypad = Toypad()
    toypad.set_tag_callbacks(tag_new=tag_new, tag_gone=tag_gone, tag_change=tag_change)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Closing ToyPad...")
    finally:
        toypad.close()
        client.loop_stop()
        client.disconnect()
