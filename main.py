import os
import json
import usb.core
import usb.util
import threading
import time
import paho.mqtt.client as mqtt
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
MQTT_HOST = os.getenv("MQTT_HOST")
MQTT_USERNAME = os.getenv("MQTT_USERNAME", None)
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", None)
CONFIG_FILE = os.getenv("CONFIG_FILE", "tags.json")
MQTT_BASE_TOPIC = os.getenv("MQTT_BASE_TOPIC", "toypad")
MASTERTAG = os.getenv("MASTERTAG", None)

ENABLE_WRITE = False
# LEGO ToyPad vendor and product IDs
VENDOR_ID = 0x0e6f
PRODUCT_ID = 0x0241
TOYPAD_INIT = [
    0x55, 0x0f, 0xb0, 0x01, 0x28, 0x63, 0x29, 0x20, 0x4c, 0x45, 0x47, 0x4f, 0x20, 0x32, 0x30, 0x31, 0x34,
    0xf7, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
]

# Message constants
MSG_NORMAL = 0x55
TAG_ADDED = 0
BGCOLOR = [12, 12, 12]

# Helper functions
def calculate_checksum(command):
    checksum = sum(command) % 256
    return checksum

def send_command(dev, command):
    checksum = calculate_checksum(command)
    message = command + [checksum]
    message += [0x00] * (32 - len(message))  # Pad to 32 bytes
    endpoint_out = dev[0][(0, 0)][1]  # Assuming endpoint 1 is for sending
    endpoint_out.write(message)

class Toypad:
    def __init__(self, vendor_id=VENDOR_ID, product_id=PRODUCT_ID):
        self.device = usb.core.find(idVendor=vendor_id, idProduct=product_id)
        if not self.device:
            raise ValueError("ToyPad not found")

        # Detach kernel driver if needed
        if self.device.is_kernel_driver_active(0):
            self.device.detach_kernel_driver(0)

        self.device.set_configuration()

        # Tag list and callbacks
        self.detected_tags = {}
        self.tagNew = None
        self.tagGone = None
        self.tagChange = None
        self.mqtt_client = mqtt.Client()

        # Load or create tag configuration
        self.tag_config = self.load_or_create_tag_config()

        # Initialize MQTT client
        self.setup_mqtt()

        # Initialize and start background thread
        self.init()
        self.listening = True
        self.listener_thread = threading.Thread(target=self.listen_for_tags)
        self.listener_thread.start()

    def init(self):
        send_command(self.device, TOYPAD_INIT)

    def setup_mqtt(self):
        if MQTT_USERNAME and MQTT_PASSWORD:
            self.mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        self.mqtt_client.connect(MQTT_HOST)
        self.mqtt_client.loop_start()

    def load_or_create_tag_config(self):
        """Load the tag configuration from a JSON file or create it if it doesn't exist."""
        if not os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "w") as file:
                json.dump({}, file)  # Initialize with an empty dictionary
            print("Created new configuration file.")
        
        try:
            with open(CONFIG_FILE, "r") as file:
                return json.load(file)
        except json.JSONDecodeError:
            print("Error loading tag configuration.")
            return {}

    def save_tag_config(self):
        """Save the current tag configuration to the JSON file."""
        with open(CONFIG_FILE, "w") as file:
            json.dump(self.tag_config, file, indent=4)

    def listen_for_tags(self):
        global ENABLE_WRITE, MASTERTAG
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
                    if uid == MASTERTAG:
                        ENABLE_WRITE = not ENABLE_WRITE
                        print("Write mode: ", ENABLE_WRITE)
                    if uid not in self.detected_tags:
                        self.detected_tags[uid] = pad_num
                        tag_info = self.get_or_create_tag_info(uid)
                        if self.tagNew:
                            self.tagNew(uid, pad_num, tag_info)
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
                if e.errno != 110:
                    print("USB Error:", e)
                    self.handle_disconnection()

    def get_or_create_tag_info(self, uid):
        """Retrieve tag info from config, create an empty entry if the tag is new."""
        if uid not in self.tag_config:
            # Create an empty entry for new tags and save to file
            self.tag_config[uid] = {"url": "", "sound": ""}
            if ENABLE_WRITE:
                self.save_tag_config()
                print(f"Created new entry for tag UID={uid}")
        return self.tag_config[uid]

    def handle_disconnection(self):
        print("ToyPad disconnected. Reconnecting...")
        self.device = None
        while not self.device:
            try:
                time.sleep(2)
                self.device = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
                if self.device:
                    self.device.set_configuration()
                    print("ToyPad reconnected.")
                    self.init()
            except usb.core.USBError:
                continue

    def set_pad_color(self, pad, color):
        command = [MSG_NORMAL, 0x06, 0xc0, 0x02, pad] + color
        send_command(self.device, command)

    def set_pad_color_fade(self, pad, color, fade_time, count=0x01):
        command = [MSG_NORMAL, 0x08, 0xc2, 0x02, pad, fade_time, count] + color
        send_command(self.device, command)

    def set_pad_color_flash(self, pad, color, on_time, off_time, count=0x02):
        command = [MSG_NORMAL, 0x09, 0xc3, 0x02, pad, on_time, off_time, count] + color
        send_command(self.device, command)

    def set_tag_callbacks(self, tag_new=None, tag_gone=None, tag_change=None):
        self.tagNew = tag_new
        self.tagGone = tag_gone
        self.tagChange = tag_change

    def close(self):
        self.listening = False
        self.listener_thread.join()
        usb.util.dispose_resources(self.device)
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()

# Example usage
if __name__ == "__main__":
    def tag_new(uid, pad, tag_info):
        toypad.set_pad_color(pad, [255, 0, 0])
        mqtt_payload = json.dumps({"uid": uid, "pad": pad, "info": tag_info})
        toypad.mqtt_client.publish(f"{MQTT_BASE_TOPIC}/tag_new", mqtt_payload)
        print(f"New tag detected: UID={uid} on Pad {pad}, Info={tag_info}")

    def tag_gone(uid, pad):
        toypad.set_pad_color(pad, BGCOLOR)
        mqtt_payload = json.dumps({"uid": uid, "pad": pad})
        toypad.mqtt_client.publish(f"{MQTT_BASE_TOPIC}/tag_gone", mqtt_payload)
        print(f"Tag removed: UID={uid} from Pad {pad}")

    def tag_change(uid, old_pad, new_pad):
        print(f"Tag moved: UID={uid} from Pad {old_pad} to Pad {new_pad}")

    toypad = Toypad()
    toypad.set_tag_callbacks(tag_new=tag_new, tag_gone=tag_gone, tag_change=tag_change)
    toypad.set_pad_color(0, BGCOLOR)
    toypad.set_pad_color(1, BGCOLOR)
    toypad.set_pad_color(2, BGCOLOR)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Closing ToyPad...")
    finally:
        toypad.close()
