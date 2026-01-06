import inspect
from neonize.client import NewClient
client = NewClient("test.db")
print(inspect.signature(client.qr))
