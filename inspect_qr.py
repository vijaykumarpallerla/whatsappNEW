from neonize.client import NewClient
client = NewClient("test.db")
print(f"client.qr: {getattr(client, 'qr', 'Not Found')}")
