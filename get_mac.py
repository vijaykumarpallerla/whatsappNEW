import uuid

def get_mac():
    mac_num = uuid.getnode()
    mac = ':'.join(['{:02x}'.format((mac_num >> ele) & 0xff)
                   for ele in range(0, 8 * 6, 8)][::-1])
    return mac.upper()

print("------------------------------------------------")
print(f"   {get_mac()}")
print("------------------------------------------------")
input("Press Enter to close...")
