import inspect
from neonize.client import NewClient

print(inspect.signature(NewClient))
print(inspect.signature(NewClient.connect))
